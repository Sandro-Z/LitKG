"""Controlled models + real PostgreSQL; never reads .env or calls a live model."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient

from app.graph_store import store_claim
from app.schemas import CatalogBundle, ClaimBundle, Claim
from app.retrieval import rebuild_derived, retrieve, answer_question, fetch_facts


@unittest.skipUnless(os.getenv('LITKG_TEST_DATABASE_URL'), 'Set disposable LITKG_TEST_DATABASE_URL')
class KnowledgeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import main, tasks, batch_status
        cls.main, cls.tasks, cls.batch_status = main, tasks, batch_status
        cls.client = TestClient(main.app)
        cls.dsn = os.environ['LITKG_TEST_DATABASE_URL']
        cls.schema = 'litkg_knowledge_' + uuid.uuid4().hex
        cls.migration = (Path(__file__).resolve().parents[1] / 'db/init.sql').read_text()
        with psycopg.connect(cls.dsn) as conn:
            conn.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
            conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        with cls.connect() as conn:
            conn.execute(cls.migration)

    @classmethod
    def connect(cls):
        return psycopg.connect(cls.dsn, row_factory=dict_row, options=f'-csearch_path={cls.schema},public')

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in [self.main, self.tasks, self.batch_status]:
            self.stack.enter_context(patch.object(module,'get_conn',side_effect=self.connect))
        self.stack.enter_context(patch.dict(os.environ, {'EMBEDDING_BASE_URL':'','EMBEDDING_MODEL':''}))
        self.kb = self.new_kb()
        self.doc = self.new_doc(self.kb)

    def new_kb(self):
        with self.connect() as conn:
            return conn.execute('INSERT INTO knowledge_bases (slug,name) VALUES (%s,%s) RETURNING id',
                                (uuid.uuid4().hex, 'test knowledge')).fetchone()['id']

    def new_doc(self, kb):
        with self.connect() as conn:
            doc = conn.execute("INSERT INTO documents (sha256,filename,source_path,title,doi,status) VALUES (%s,'test.pdf','/test/source.pdf','Controlled study','10.1234/controlled','done') RETURNING id", (uuid.uuid4().hex,)).fetchone()['id']
            conn.execute('INSERT INTO knowledge_base_documents (knowledge_base_id,document_id) VALUES (%s,%s)',(kb,doc))
            return doc

    def chunk(self, doc, text, index=0):
        with self.connect() as conn:
            return conn.execute("INSERT INTO chunks (document_id,chunk_index,text,section_title,page_start,page_end,page_spans,status) VALUES (%s,%s,%s,'Results',3,3,%s,'queued') RETURNING id",
                                (doc,index,text,Jsonb([{'start':0,'end':len(text),'page_start':3,'page_end':3}]))).fetchone()['id']

    def store(self, doc, chunk, sentence, **extra):
        data = {'subject':{'text':'Probe Q','type':'Drug'},'predicate':'REDUCES','object':{'text':'IL-6','type':'Protein'},
                'evidence':{'sentence':sentence,'page':3,'page_end':3},'confidence':.9, **extra}
        with self.connect() as conn:
            return store_claim(conn.cursor(),document_id=doc,chunk_id=chunk,claim=Claim.model_validate(data),extraction_model='controlled')

    def test_catalog_failure_resumes_segments_before_any_relationship_is_queued(self):
        texts = ['Interleukin-6 (IL-6) was studied.', 'Probe Q was used in the final paragraph.']
        chunks = [self.chunk(self.doc,t,i) for i,t in enumerate(texts)]
        calls = []
        fail = [True]
        def model(section,text):
            calls.append(text)
            if text == texts[1] and fail[0]:
                raise ValueError('controlled catalog failure')
            name, typ = ('Interleukin-6','Protein') if text == texts[0] else ('Probe Q','Drug')
            return CatalogBundle.model_validate({'entities':[{'text':name,'type':typ,'evidence':text}]})
        with patch.object(self.tasks,'extract_catalog',side_effect=model), patch.object(self.tasks.extract_chunk,'apply_async') as publish:
            with self.assertRaisesRegex(ValueError,'controlled'):
                self.tasks.build_document_catalog.run(self.doc)
            publish.assert_not_called()
            with self.connect() as conn:
                doc = conn.execute('SELECT catalog_status,status FROM documents WHERE id=%s',(self.doc,)).fetchone()
                self.assertEqual(doc,{'catalog_status':'failed','status':'failed'})
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM processing_cache WHERE document_id=%s AND stage='catalog'",(self.doc,)).fetchone()['n'],1)
            fail[0] = False
            def before_publish(*args,**kwargs):
                with self.connect() as conn:
                    doc = conn.execute('SELECT catalog_status FROM documents WHERE id=%s',(self.doc,)).fetchone()
                    n = conn.execute('SELECT count(*) AS n FROM document_entities WHERE document_id=%s',(self.doc,)).fetchone()['n']
                self.assertEqual(doc['catalog_status'],'done')
                self.assertEqual(n,2)
            publish.side_effect = before_publish
            result = self.tasks.build_document_catalog.run(self.doc)
            self.assertEqual(result['chunk_count'],2)
            self.assertEqual(publish.call_count,2)
        self.assertEqual(calls.count(texts[0]),1)
        self.assertEqual(calls.count(texts[1]),2)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM processing_attempts WHERE document_id=%s AND status='failed'",(self.doc,)).fetchone()['n'],1)
        # A stale generation is ignored before any model work or mutation.
        with patch.object(self.tasks,'extract_claims',side_effect=AssertionError('stale task called model')):
            self.assertEqual(self.tasks.extract_chunk.run(chunks[0],'old-version',-1)['status'],'superseded')

    def test_directory_gate_prevents_direct_relationship_extraction(self):
        chunk = self.chunk(self.doc,'Probe Q reduces IL-6.')
        with patch.object(self.tasks,'extract_claims') as model:
            with self.assertRaisesRegex(ValueError,'complete document catalog'):
                self.tasks.extract_chunk.run(chunk)
        model.assert_not_called()
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE document_id=%s',(self.doc,)).fetchone()['n'],0)

    def test_title_is_catalogued_with_honest_location_and_old_versions_are_hidden(self):
        sentence='Probe Q reduces IL-6.'
        chunk=self.chunk(self.doc,sentence)
        self.store(self.doc,chunk,sentence)
        with self.connect() as conn:
            rebuild_derived(conn.cursor(),self.kb)
            conn.execute("UPDATE documents SET title='EGFR binding study',catalog_version='new',catalog_status='done' WHERE id=%s",(self.doc,))
        self.assertEqual(self.client.get(f'/knowledge-bases/{self.kb}/relations').json()['total'],0)
        with self.connect() as conn:
            self.assertEqual(retrieve(conn.cursor(),self.kb,'IL-6')['citations'],[])
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE document_id=%s',(self.doc,)).fetchone()['n'],1)
        catalog=CatalogBundle.model_validate({'entities':[{'text':'EGFR','type':'Protein','evidence':'EGFR binding study'}]})
        with patch.object(self.tasks,'extract_catalog',return_value=catalog):
            self.tasks.build_document_catalog.run(self.doc,publish=False)
        entries=self.client.get(f'/knowledge-bases/{self.kb}/documents/{self.doc}/catalog').json()['entities']
        self.assertEqual(entries[0]['locations'][0]['source_kind'],'document_title')
        self.assertIsNone(entries[0]['locations'][0]['page'])

    def test_multichunk_identity_processes_roles_negation_and_all_evidence(self):
        texts = ['Interleukin-6 (IL-6) was measured.',
                 'Probe Q decreased IL-6 secretion in A549 cells at 5 nM for 24 h. This effect may occur in humans.',
                 'Probe Q did not reduce IL-6 expression in A549 cells at 48 h.']
        chunk_ids = [self.chunk(self.doc,t,i) for i,t in enumerate(texts)]
        def directory(section,text):
            items = [{'text':'Interleukin-6' if text == texts[0] else 'IL-6','type':'Protein','evidence':text}]
            if 'Probe Q' in text:
                items.append({'text':'Probe Q','type':'Drug','evidence':text})
            # A549 and humans are deliberately missed by the initial directory.
            return CatalogBundle.model_validate({'entities':items})
        def model(section_title,chunk_text,directory):
            if chunk_text == texts[0]:
                return ClaimBundle(claims=[])
            secretion = chunk_text == texts[1]
            return ClaimBundle.model_validate({'claims':[{
                'subject':{'text':'Probe Q','type':'Drug'},'predicate':'REDUCES',
                'object':{'text':'IL-6','type':'Protein','mention':'IL-6 secretion' if secretion else 'IL-6 expression',
                          'process':{'kind':'secretion' if secretion else 'expression','direction':'decreased' if secretion else 'unchanged'}},
                'participants':[{'role':'cell_line','entity':{'text':'A549','type':'CellLine'}}] + ([{'role':'species','entity':{'text':'humans','type':'Organism'}}] if secretion else []),
                'qualifiers':{'dose':5,'unit':'nM','time':'24 h' if secretion else '48 h'},
                'evidence':{'sentence':texts[1].split('. ')[0]+'.' if secretion else texts[2]},
                'additional_evidence':[{'sentence':'This effect may occur in humans.'}] if secretion else [],
                'confidence':.8,'negated':not secretion,'speculative':secretion}]})
        with patch.object(self.tasks,'extract_catalog',side_effect=directory), patch.object(self.tasks,'extract_claims',side_effect=model) as model_calls:
            catalog = self.tasks.build_document_catalog.run(self.doc,publish=False)
            for c in chunk_ids:
                self.tasks.extract_chunk.run(c,catalog['catalog_version'],0,refresh=False)
            with self.connect() as conn:
                originals = conn.execute('SELECT id,claim_json FROM claims WHERE document_id=%s ORDER BY id',(self.doc,)).fetchall()
            # A second delivery uses validated caches and preserves claim IDs.
            for c in chunk_ids:
                self.tasks.extract_chunk.run(c,catalog['catalog_version'],0,refresh=False)
            self.assertEqual(model_calls.call_count,3)
        with self.connect() as conn:
            cur = conn.cursor()
            facts = fetch_facts(cur,self.kb,document_id=self.doc)
            self.assertEqual(len(facts),2)
            self.assertEqual(len(facts[0]['evidence']),2)
            self.assertEqual([f['negated'] for f in facts],[False,True])
            self.assertEqual([f['speculative'] for f in facts],[True,False])
            self.assertEqual({p['role'] for p in facts[0]['participants']},{'subject','object','cell_line','species'})
            self.assertEqual({f['qualifiers']['processes'][0]['kind'] for f in facts},{'expression','secretion'})
            ids = {p['entity_id'] for f in facts for p in f['participants'] if p['role']=='object'}
            self.assertEqual(len(ids),1)
            self.assertEqual(conn.execute('SELECT id,claim_json FROM claims WHERE document_id=%s ORDER BY id',(self.doc,)).fetchall(), originals)
            self.assertTrue(all(e['page']==3 for f in facts for e in f['evidence']))
            self.assertGreater(conn.execute("SELECT count(*) AS n FROM review_items WHERE document_id=%s AND kind='catalog_omission'",(self.doc,)).fetchone()['n'],0)
            changed = rebuild_derived(cur,self.kb,[self.doc])
            second = rebuild_derived(cur,self.kb,[self.doc])
            self.assertGreater(changed['changed_count'],0)
            self.assertEqual(second['changed_count'],0)
            result = retrieve(cur,self.kb,'IL-6 secretion',limit=5,hops=2)
            self.assertEqual(len(result['facts']),2)
            self.assertTrue(all(c['doi']=='10.1234/controlled' and c['page']==3 for c in result['citations']))

    def test_dual_retrieval_semantic_optional_and_knowledge_base_isolation(self):
        sentence = 'Probe Q reduces IL-6 in the controlled study.'
        c = self.chunk(self.doc,sentence)
        self.store(self.doc,c,sentence,qualifiers={'time':'24 h'})
        other_kb = self.new_kb()
        other_doc = self.new_doc(other_kb)
        other_sentence = 'Private-only evidence in another KB about IL-6.'
        other_c = self.chunk(other_doc,other_sentence)
        self.store(other_doc,other_c,other_sentence,qualifiers={'time':'48 h'})
        with self.connect() as conn:
            cur=conn.cursor()
            rebuild_derived(cur,self.kb)
            rebuild_derived(cur,other_kb)
            result=retrieve(cur,self.kb,'IL-6',hops=2)
            self.assertTrue(result['citations'])
            self.assertEqual({c['document_id'] for c in result['citations']},{self.doc})
            self.assertNotIn('Private-only',str(result))
            answer=answer_question(result,'IL-6',False)
            self.assertEqual(answer['status'],'evidence_only')
        response=self.client.get(f'/knowledge-bases/{self.kb}/documents/{other_doc}/catalog')
        self.assertEqual(response.status_code,404)
        # Index a controlled vector, then ask a question with no lexical overlap.
        from app import retrieval
        with patch.dict(os.environ,{'EMBEDDING_BASE_URL':'http://controlled/v1','EMBEDDING_MODEL':'controlled'}), patch.object(retrieval,'embed',return_value=([1.,0.,0.],retrieval.digest(['http://controlled/v1','controlled']))):
            with self.connect() as conn:
                rebuild_derived(conn.cursor(),self.kb)
                result=retrieve(conn.cursor(),self.kb,'lexically-unrelated-question',hops=1)
                self.assertTrue(result['citations'])
                self.assertEqual({c['document_id'] for c in result['citations']},{self.doc})
        with patch.dict(os.environ,{'EMBEDDING_BASE_URL':'http://controlled/v1','EMBEDDING_MODEL':'controlled'}), patch.object(retrieval,'embed',side_effect=ValueError('controlled embedding failure')):
            with self.connect() as conn:
                result=retrieve(conn.cursor(),self.kb,'IL-6')
                self.assertTrue(result['citations'])
                self.assertTrue(result['warnings'])

    def test_concurrent_redelivery_is_idempotent_for_claim_fact_and_roles(self):
        sentence='Probe Q reduces IL-6.'
        chunk=self.chunk(self.doc,sentence)
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(lambda _:self.store(self.doc,chunk,sentence,participants=[{'role':'cell_line','entity':{'text':'A549','type':'CellLine'}}]),range(4)))
        self.assertEqual(len(set(values)),1)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM facts f JOIN claims c ON c.id=f.claim_id WHERE c.document_id=%s',(self.doc,)).fetchone()['n'],1)

    def test_process_mentions_remain_traceable_without_splitting_relation_identity(self):
        relations=[]
        for index,mention in enumerate(['IL-6 secretion','IL6 secretion']):
            sentence=f'Probe Q reduces {mention}.'
            chunk=self.chunk(self.doc,sentence,index)
            _,relation=self.store(self.doc,chunk,sentence,object={'text':'IL-6','type':'Protein','mention':mention,
                'process':{'kind':'secretion','direction':'decreased'}})
            relations.append(relation)
        self.assertEqual(len(set(relations)),1)
        with self.connect() as conn:
            facts=fetch_facts(conn.cursor(),self.kb)
            self.assertEqual({p['mention'] for f in facts for p in f['participants'] if p['role']=='object'}, {'IL-6 secretion','IL6 secretion'})

    def test_legacy_reprojection_is_llm_free_preserves_raw_and_migration_is_idempotent(self):
        sentence='Probe Q reduces IL-6 expression.'
        chunk=self.chunk(self.doc,sentence)
        claim_id,_=self.store(self.doc,chunk,sentence,object={'text':'IL-6 expression','type':'Other'},negated=True,speculative=True)
        with self.connect() as conn:
            conn.execute("UPDATE chunks SET status='done' WHERE id=%s",(chunk,))
            raw=conn.execute('SELECT claim_json FROM claims WHERE id=%s',(claim_id,)).fetchone()['claim_json']
            conn.execute('DELETE FROM facts WHERE claim_id=%s',(claim_id,))
            conn.execute(self.migration)
            conn.execute(self.migration)
            # Reapplying migration must not import unrelated KB documents into default.
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM knowledge_base_documents m JOIN knowledge_bases k ON k.id=m.knowledge_base_id WHERE k.slug='default' AND m.document_id=%s",(self.doc,)).fetchone()['n'],0)
        with patch.object(self.tasks.maintain_knowledge_base,'apply_async'):
            response=self.client.post(f'/knowledge-bases/{self.kb}/maintenance',json={'mode':'reproject','document_id':self.doc})
        self.assertEqual(response.status_code,202)
        job=response.json()
        with patch.object(self.tasks,'extract_claims',side_effect=AssertionError('Must not call LLM')),patch.object(self.tasks,'extract_catalog',side_effect=AssertionError('Must not call LLM')):
            result=self.tasks.maintain_knowledge_base.run(job['task_id'])
        self.assertEqual(result['status'],'done',str(result))
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT claim_json FROM claims WHERE id=%s',(claim_id,)).fetchone()['claim_json'],raw)
            fact=fetch_facts(conn.cursor(),self.kb)[0]
            self.assertTrue(fact['negated'] and fact['speculative'])
            self.assertEqual(len(fact['evidence']),1)
            self.assertGreater(conn.execute('SELECT count(*) AS n FROM claim_history WHERE document_id=%s',(self.doc,)).fetchone()['n'],0)
        reviews=self.client.get(f'/knowledge-bases/{self.kb}/reviews').json()['reviews']
        self.assertTrue(any(r['kind']=='semantic_split' for r in reviews))
        self.assertEqual(self.client.get(job['status_url']).json()['status'],'done')

    def test_bad_evidence_fails_visibly_without_caching_empty_success(self):
        sentence='Probe Q reduces IL-6.'
        c=self.chunk(self.doc,sentence)
        with patch.object(self.tasks,'extract_catalog',return_value=CatalogBundle(entities=[])):
            catalog=self.tasks.build_document_catalog.run(self.doc,publish=False)
        invalid=ClaimBundle.model_validate({'claims':[{'subject':{'text':'Probe Q','type':'Drug'},'object':{'text':'IL-6','type':'Protein'},'predicate':'REDUCES','evidence':{'sentence':'Fabricated sentence.'},'confidence':.9}]})
        with patch.object(self.tasks,'extract_claims',return_value=invalid):
            with self.assertRaisesRegex(ValueError,'evidence'):
                self.tasks.extract_chunk.run(c,catalog['catalog_version'],0,refresh=False)
        status=self.client.get(f'/knowledge-bases/{self.kb}/documents/{self.doc}/processing').json()
        self.assertEqual(status['chunks'][0]['status'],'failed')
        self.assertIn('evidence',status['chunks'][0]['error'])
        self.assertTrue(any(a['stage']=='claims' and a['status']=='failed' for a in status['attempts']))
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM processing_cache WHERE document_id=%s AND stage='claims'",(self.doc,)).fetchone()['n'],0)

    def test_maintenance_reextract_and_retry_reuse_the_same_forced_revision(self):
        sentence='Probe Q reduces IL-6.'
        self.chunk(self.doc,sentence)
        catalog=CatalogBundle.model_validate({'entities':[{'text':'Probe Q','type':'Drug','evidence':sentence},
                                                        {'text':'IL-6','type':'Protein','evidence':sentence}]})
        claim=ClaimBundle.model_validate({'claims':[{'subject':{'text':'Probe Q','type':'Drug'},'predicate':'REDUCES',
            'object':{'text':'IL-6','type':'Protein'},'evidence':{'sentence':sentence},'confidence':.9}]})
        def job(mode):
            job_id=str(uuid.uuid4())
            with self.connect() as conn:
                conn.execute('INSERT INTO maintenance_jobs (id,knowledge_base_id,document_id,mode) VALUES (%s,%s,%s,%s)',(job_id,self.kb,self.doc,mode))
            return job_id
        with patch.object(self.tasks,'extract_catalog',return_value=catalog) as catalog_model, patch.object(self.tasks,'extract_claims',side_effect=ValueError('controlled extraction error')):
            first=self.tasks.maintain_knowledge_base.run(job('reextract'))
        self.assertEqual(first['status'],'partial')
        self.assertEqual(catalog_model.call_count,1)
        with self.connect() as conn:
            before=conn.execute('SELECT catalog_version,extraction_epoch FROM documents WHERE id=%s',(self.doc,)).fetchone()
        with patch.object(self.tasks,'extract_catalog',side_effect=AssertionError('Directory already completed')), patch.object(self.tasks,'extract_claims',return_value=claim) as extraction:
            second=self.tasks.maintain_knowledge_base.run(job('retry'))
        self.assertEqual(second['status'],'done',str(second))
        self.assertEqual(extraction.call_count,1)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT catalog_version,extraction_epoch FROM documents WHERE id=%s',(self.doc,)).fetchone(),before)
            raw=conn.execute('SELECT claim_json FROM claims WHERE document_id=%s',(self.doc,)).fetchone()['claim_json']
        # Reprojection of the original model names still resolves through the directory.
        with patch.object(self.tasks,'extract_claims',side_effect=AssertionError('Reproject called model')):
            third=self.tasks.maintain_knowledge_base.run(job('reproject'))
        self.assertEqual(third['status'],'done',str(third))
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT claim_json FROM claims WHERE document_id=%s',(self.doc,)).fetchone()['claim_json'],raw)

    def test_conflicts_are_scoped_conditions_remain_distinct_and_evaluation_requires_labels(self):
        other=self.new_doc(self.kb)
        sentence='Aspirin reduces IL-6 in A549.'
        c=self.chunk(self.doc,sentence)
        other_c=self.chunk(other,'Aspirin did not reduce IL-6 in A549.')
        subject={'text':'Aspirin','type':'Drug'}
        a,_=self.store(self.doc,c,sentence,subject=subject,qualifiers={'cell_line':'A549','time':'24 h'})
        self.store(other,other_c,'Aspirin did not reduce IL-6 in A549.',subject=subject,negated=True,qualifiers={'cell_line':'A549','time':'24 h'})
        self.store(other,other_c,'Aspirin did not reduce IL-6 in A549.',subject=subject,negated=True,qualifiers={'cell_line':'A549','time':'48 h'})
        from app.evaluation import evaluate
        with self.connect() as conn:
            rebuild_derived(conn.cursor(),self.kb)
            facts=fetch_facts(conn.cursor(),self.kb)
            self.assertEqual(len(facts),3)
            self.assertTrue(all('cell_line' in {p['role'] for p in f['participants']} for f in facts))
            labelled=evaluate(conn.cursor(),self.kb,[{'question':'IL-6','expected_claim_ids':[a]}])
            unlabelled=evaluate(conn.cursor(),self.kb,[{'question':'IL-6'}])
            self.assertEqual(labelled['recall'],1.0)
            self.assertEqual(labelled['citation_error_rate'],0.0)
            self.assertIsNone(unlabelled['recall'])
        reviews=self.client.get(f'/knowledge-bases/{self.kb}/reviews').json()['reviews']
        cross=[r for r in reviews if r['detail'].get('knowledge_base_id')==self.kb]
        self.assertTrue(cross)
        pairs={tuple(r['detail']['fact_ids']) for r in cross}
        self.assertEqual(len(pairs),1)  # The 48 h result is a different condition.
