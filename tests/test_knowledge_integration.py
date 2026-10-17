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

    def maintenance_job(self, mode='retry'):
        job_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute('INSERT INTO maintenance_jobs (id,knowledge_base_id,document_id,mode) VALUES (%s,%s,%s,%s)',
                         (job_id,self.kb,self.doc,mode))
        return job_id

    def store(self, doc, chunk, sentence, **extra):
        data = {'subject':{'text':'Probe Q','type':'Drug'},'predicate':'REDUCES','object':{'text':'IL-6','type':'Protein'},
                'evidence':{'sentence':sentence,'page':3,'page_end':3},'confidence':.9, **extra}
        with self.connect() as conn:
            return store_claim(conn.cursor(),document_id=doc,chunk_id=chunk,claim=Claim.model_validate(data),extraction_model='controlled')

    def test_knowledge_base_rename_and_delete_cascade_preserve_shared_documents(self):
        other = self.new_kb()
        graph_job = str(uuid.uuid4())
        maintenance_job = str(uuid.uuid4())
        with self.connect() as conn:
            original = conn.execute('SELECT slug FROM knowledge_bases WHERE id=%s', (self.kb,)).fetchone()
            conn.execute('INSERT INTO knowledge_base_documents (knowledge_base_id,document_id) VALUES (%s,%s)', (other,self.doc))
            batch = conn.execute('INSERT INTO ingestion_batches (knowledge_base_id,name) VALUES (%s,\'delete test\') RETURNING id', (self.kb,)).fetchone()['id']
            conn.execute('INSERT INTO ingestion_batch_items (batch_id,position,filename,document_id) VALUES (%s,0,\'shared.pdf\',%s)', (batch,self.doc))
            conn.execute('INSERT INTO graph_rebuild_jobs (id,knowledge_base_id) VALUES (%s,%s)', (graph_job,self.kb))
            conn.execute('INSERT INTO maintenance_jobs (id,knowledge_base_id,mode) VALUES (%s,%s,\'derived\')', (maintenance_job,self.kb))
            for key, detail in [('delete-review', {'knowledge_base_id':self.kb}),
                                ('keep-review', {'knowledge_base_id':other}), ('global-review', {})]:
                conn.execute("INSERT INTO review_items (document_id,version,item_key,kind,detail) VALUES (%s,'test',%s,'semantic_conflict',%s)",
                             (self.doc,key,Jsonb(detail)))
            rebuild_derived(conn.cursor(), self.kb)
            rebuild_derived(conn.cursor(), other)
        renamed = self.client.put(f'/knowledge-bases/{self.kb}', json={'name':' 修改后的库 ', 'description':' 新说明 '})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()['slug'], original['slug'])
        self.assertEqual(renamed.json()['description'], '新说明')
        mismatch = self.client.delete(f'/knowledge-bases/{self.kb}', params={'confirm_name':'test knowledge'})
        self.assertEqual(mismatch.status_code, 409)
        response = self.client.delete(f'/knowledge-bases/{self.kb}', params={'confirm_name':'修改后的库'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f'/knowledge-bases/{self.kb}').status_code, 404)
        with self.connect() as conn:
            for table in ['knowledge_base_documents','ingestion_batches','graph_rebuild_jobs','maintenance_jobs','derived_content']:
                self.assertEqual(conn.execute(f'SELECT count(*) AS n FROM {table} WHERE knowledge_base_id=%s', (self.kb,)).fetchone()['n'], 0)
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM ingestion_batch_items WHERE batch_id=%s', (batch,)).fetchone()['n'], 0)
            self.assertIsNotNone(conn.execute('SELECT id FROM documents WHERE id=%s', (self.doc,)).fetchone())
            self.assertIsNotNone(conn.execute('SELECT 1 FROM knowledge_base_documents WHERE knowledge_base_id=%s AND document_id=%s', (other,self.doc)).fetchone())
            self.assertGreater(conn.execute('SELECT count(*) AS n FROM derived_content WHERE knowledge_base_id=%s', (other,)).fetchone()['n'], 0)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM review_items WHERE item_key='delete-review'").fetchone()['n'], 0)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM review_items WHERE item_key IN ('keep-review','global-review')").fetchone()['n'], 2)
            # A queued worker must skip a deleted KB instead of failing on its FK.
            self.assertEqual(rebuild_derived(conn.cursor(), self.kb)['changed_count'], 0)
        self.assertEqual(self.client.delete(f'/knowledge-bases/{self.kb}', params={'confirm_name':'修改后的库'}).status_code, 404)

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

    def test_catalog_truncation_splits_and_child_failure_resumes_before_publication(self):
        from collections import Counter
        source='Alpha protein was measured. Beta protein was measured. Gamma protein was measured. Delta protein was measured.'
        self.chunk(self.doc,source)
        calls=Counter()
        failing=[True]
        def model(heading,text,**kwargs):
            self.assertEqual(kwargs,{})
            calls[text]+=1
            if len(text)>80:
                raise self.tasks.ModelOutputTruncatedError('controlled capacity limit','partial JSON')
            if 'Delta' in text and failing[0]:
                raise ValueError('controlled child failure')
            from app.catalog import source_contains
            return CatalogBundle.model_validate({'entities':[{'text':name,'type':'Protein','evidence':name}
                for name in ['Alpha protein','Beta protein','Gamma protein','Delta protein'] if source_contains(text,name)]})
        def published(*args,**kwargs):
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT catalog_status FROM documents WHERE id=%s',(self.doc,)).fetchone()['catalog_status'],'done')
                self.assertEqual(conn.execute('SELECT count(*) AS n FROM document_entities WHERE document_id=%s',(self.doc,)).fetchone()['n'],4)
        with patch.dict(os.environ,{'CATALOG_MIN_SEGMENT_CHARS':'20'}),patch.object(self.tasks,'extract_catalog',side_effect=model),patch.object(self.tasks.extract_chunk,'apply_async',side_effect=published) as publish:
            with self.assertRaisesRegex(ValueError,'child failure'):
                self.tasks.build_document_catalog.run(self.doc)
            publish.assert_not_called()
            cached_texts=[text for text in calls if text!=source and 'Delta' not in text]
            self.assertTrue(cached_texts)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT catalog_status FROM documents WHERE id=%s',(self.doc,)).fetchone()['catalog_status'],'failed')
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM processing_cache WHERE document_id=%s AND stage='catalog_split'",(self.doc,)).fetchone()['n'],1)
            failing[0]=False
            result=self.tasks.build_document_catalog.run(self.doc)
        self.assertEqual(result['entity_count'],4)
        publish.assert_called_once()
        self.assertEqual(calls[source],1)
        self.assertTrue(all(calls[text]==1 for text in cached_texts))

    def test_directory_gate_prevents_direct_relationship_extraction(self):
        chunk = self.chunk(self.doc,'Probe Q reduces IL-6.')
        with patch.object(self.tasks,'extract_claims') as model:
            with self.assertRaisesRegex(ValueError,'complete document catalog'):
                self.tasks.extract_chunk.run(chunk)
        model.assert_not_called()
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE document_id=%s',(self.doc,)).fetchone()['n'],0)

    def test_catalog_repair_persists_clean_cache_original_output_and_review(self):
        text = 'Probe Q reduces IL-6.'
        self.chunk(self.doc, text)
        value = CatalogBundle.model_validate({'entities':[{'text':'IL-6','type':'Protein',
            'aliases':['Invented alias'], 'evidence':'IL-6 was reduced by Probe Q.'}]})
        value._raw_output = 'original controlled response'
        with patch.object(self.tasks,'extract_catalog',return_value=value) as model, patch.object(self.tasks.extract_chunk,'apply_async') as publish:
            self.tasks.build_document_catalog.run(self.doc)
            self.tasks.build_document_catalog.run(self.doc)
        model.assert_called_once()
        self.assertEqual(publish.call_count,2)
        with self.connect() as conn:
            payload = conn.execute("SELECT payload FROM processing_cache WHERE document_id=%s AND stage='catalog'",(self.doc,)).fetchone()['payload']
            self.assertEqual(payload['entities'][0]['aliases'],[])
            self.assertEqual(payload['entities'][0]['evidence'],text)
            audit = conn.execute("SELECT output FROM processing_attempts WHERE document_id=%s AND status='done'",(self.doc,)).fetchone()['output']
            self.assertEqual(audit['raw'],'original controlled response')
            self.assertEqual(audit['original_parsed']['entities'][0]['aliases'],['Invented alias'])
            self.assertGreater(conn.execute("SELECT count(*) AS n FROM review_items WHERE document_id=%s AND kind='catalog_repair'",(self.doc,)).fetchone()['n'],0)

    def test_catalog_semantic_retry_completes_before_relationship_publication(self):
        text = 'Probe Q reduces IL-6.'
        self.chunk(self.doc,text)
        calls = []
        def model(section,source,**kwargs):
            calls.append(kwargs)
            if not kwargs:
                return CatalogBundle.model_validate({'entities':[{'text':'Invented protein','type':'Protein','evidence':text}]})
            self.assertIn('entity[0]',kwargs['feedback'])
            self.assertEqual(kwargs['previous_output']['entities'][0]['text'],'Invented protein')
            return CatalogBundle.model_validate({'entities':[{'text':'IL-6','type':'Protein','evidence':text}]})
        def published(*args,**kwargs):
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT catalog_status FROM documents WHERE id=%s',(self.doc,)).fetchone()['catalog_status'],'done')
        with patch.dict(os.environ,{'CATALOG_VALIDATION_ATTEMPTS':'3'}), patch.object(self.tasks,'extract_catalog',side_effect=model), patch.object(self.tasks.extract_chunk,'apply_async',side_effect=published) as publish:
            self.tasks.build_document_catalog.run(self.doc)
        self.assertEqual(len(calls),2)
        publish.assert_called_once()
        with self.connect() as conn:
            statuses = conn.execute('SELECT status FROM processing_attempts WHERE document_id=%s ORDER BY id',(self.doc,)).fetchall()
            self.assertEqual([row['status'] for row in statuses],['failed','done'])

    def test_catalog_quarantines_hallucinated_entry_and_resumes_with_visible_review(self):
        text = 'Agencourt AMPure XP beads (Beckman Coulter) and sequenced. PCSK9 was measured.'
        chunk = self.chunk(self.doc,text)
        output = CatalogBundle.model_validate({'entities':[
            {'text':'Agencourt AMPure XP beads','type':'Compound','mention':'Agencourt AMPure XP beads','evidence':text},
            {'text':'Beckman Coulson','type':'Other','mention':'Beckman Coulson',
             'evidence':'Agencourt AMPure XP beads (Beckman Coulson) and sequenced.'},
            {'text':'PCSK9','type':'Protein','mention':'PCSK9','evidence':'PCSK9 was measured.'}]})
        output._raw_output = 'original response with Beckman Coulson'
        def published(*args,**kwargs):
            self.assertEqual(kwargs['args'][0],chunk)
            state = self.client.get(f'/knowledge-bases/{self.kb}/documents/{self.doc}/catalog').json()
            self.assertEqual(state['catalog_status'],'done')
            self.assertEqual(state['catalog_quality'], {'status':'needs_review','rejected_candidates':1,'pending_rejections':1})
            self.assertEqual({e['text'] for e in state['entities']},{'Agencourt AMPure XP beads','PCSK9'})
        with patch.object(self.tasks,'extract_catalog',return_value=output) as model, patch.object(self.tasks.extract_chunk,'apply_async',side_effect=published) as publish:
            first = self.tasks.build_document_catalog.run(self.doc)
            second = self.tasks.build_document_catalog.run(self.doc)
        self.assertEqual(first,second)
        model.assert_called_once()
        self.assertEqual(publish.call_count,2)
        self.assertEqual(first['rejected_candidates'],1)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM document_entities WHERE document_id=%s',(self.doc,)).fetchone()['n'],2)
            attempts = conn.execute('SELECT status,output FROM processing_attempts WHERE document_id=%s',(self.doc,)).fetchall()
            self.assertEqual(len(attempts),1)
            self.assertEqual(attempts[0]['status'],'partial')
            self.assertEqual(attempts[0]['output']['raw'],output._raw_output)
            self.assertEqual(attempts[0]['output']['rejections'][0]['entity']['text'],'Beckman Coulson')
        reviews = self.client.get(f'/knowledge-bases/{self.kb}/reviews').json()['reviews']
        rejected = [r for r in reviews if r['kind']=='catalog_rejection']
        self.assertEqual(len(rejected),1)
        self.assertIn('Beckman Coulter',rejected[0]['detail']['sources'][-1])
        self.assertEqual(rejected[0]['detail']['chunk_id'],chunk)
        self.assertEqual(rejected[0]['detail']['entity']['text'],'Beckman Coulson')
        other_kb = self.new_kb()
        self.assertEqual(self.client.get(f'/knowledge-bases/{other_kb}/reviews').json()['reviews'],[])
        status = self.client.get(f'/knowledge-bases/{self.kb}/documents/{self.doc}/processing').json()
        self.assertEqual(status['catalog_quality']['rejected_candidates'],1)

    def test_all_ungrounded_catalog_entries_still_fail_and_never_publish_relationships(self):
        self.chunk(self.doc,'PCSK9 was measured.')
        output = CatalogBundle.model_validate({'entities':[{'text':'Invented protein','type':'Protein','evidence':'Invented protein'}]})
        with patch.object(self.tasks,'extract_catalog',return_value=output) as model, patch.object(self.tasks.extract_chunk,'apply_async') as publish:
            with self.assertRaisesRegex(self.tasks.CatalogValidationError,'no grounded catalog entries'):
                self.tasks.build_document_catalog.run(self.doc)
        self.assertEqual(model.call_count,3)
        publish.assert_not_called()
        status = self.client.get(f'/knowledge-bases/{self.kb}/documents/{self.doc}/processing').json()
        self.assertEqual(status['document']['catalog_status'],'failed')
        self.assertEqual(status['catalog_quality']['status'],'not_complete')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM processing_cache WHERE document_id=%s',(self.doc,)).fetchone()['n'],0)

    def test_quarantine_metadata_survives_later_segment_failure_and_cache_resume(self):
        texts = ['PCSK9 was measured.', 'A549 was measured.']
        for i,text in enumerate(texts):
            self.chunk(self.doc,text,i)
        first = CatalogBundle.model_validate({'entities':[
            {'text':'PCSK9','type':'Protein','evidence':texts[0]},
            {'text':'Invented protein','type':'Protein','evidence':'Invented protein'}]})
        with patch.object(self.tasks,'extract_catalog',side_effect=[first, ValueError('controlled later failure')]):
            with self.assertRaisesRegex(ValueError,'controlled later failure'):
                self.tasks.build_document_catalog.run(self.doc,publish=False)
        last = CatalogBundle.model_validate({'entities':[{'text':'A549','type':'CellLine','evidence':texts[1]}]})
        with patch.object(self.tasks,'extract_catalog',return_value=last) as model:
            result = self.tasks.build_document_catalog.run(self.doc,publish=False)
        model.assert_called_once()
        self.assertEqual(result['entity_count'],2)
        status = self.client.get(f'/knowledge-bases/{self.kb}/documents/{self.doc}/catalog').json()
        self.assertEqual(status['catalog_quality']['rejected_candidates'],1)

    def test_existing_grounded_segment_cache_is_revalidated_without_model_call(self):
        from app.catalog import digest
        text = 'PCSK9 was measured.'
        self.chunk(self.doc,text)
        payload = CatalogBundle.model_validate({'entities':[{'text':'PCSK9','type':'Protein','evidence':text}]}).model_dump(mode='json')
        # Exact key used by deployed 17a86d9, before the quarantine policy.
        key = digest(['catalog-v2-grounded-repair', self.tasks.CATALOG_PROMPT_VERSION,
                      self.tasks.TYPE_DEFINITIONS,self.tasks.MODEL_SIGNATURE,
                      'Document title: Controlled study\nSection: Results',text,0])
        with self.connect() as conn:
            conn.execute("INSERT INTO processing_cache (document_id,stage,cache_key,payload) VALUES (%s,'catalog',%s,%s)",
                         (self.doc,key,Jsonb(payload)))
        with patch.object(self.tasks,'extract_catalog',side_effect=AssertionError('Must reuse validated cache')):
            result = self.tasks.build_document_catalog.run(self.doc,publish=False)
        self.assertEqual(result['entity_count'],1)
        self.assertEqual(result['rejected_candidates'],0)

    def test_superseded_maintenance_stops_without_finalizing_the_new_generation(self):
        for i in range(2):
            self.chunk(self.doc,f'Protein {i} was measured.',i)
        with patch.object(self.tasks,'extract_catalog',return_value=CatalogBundle(entities=[])):
            self.tasks.build_document_catalog.run(self.doc,publish=False)
        def supersede(*args,**kwargs):
            with self.connect() as conn:
                conn.execute("UPDATE documents SET pipeline_generation=pipeline_generation+1,status='cataloging',catalog_status='pending' WHERE id=%s",(self.doc,))
            return {'status':'superseded'}
        with patch.object(self.tasks.extract_chunk,'run',side_effect=supersede) as extract, patch.object(self.tasks,'refresh_document_status',side_effect=AssertionError('Old job must not finalize new generation')):
            result = self.tasks.maintain_knowledge_base.run(self.maintenance_job())
        extract.assert_called_once()
        self.assertEqual(result['documents'][0]['status'],'superseded')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM documents WHERE id=%s',(self.doc,)).fetchone()['status'],'cataloging')

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

    def test_partial_claims_persist_and_retry_closes_shared_batch(self):
        from app.schemas import Claim
        from test_claims_robustness import claim, SOURCE
        chunk = self.chunk(self.doc,SOURCE)
        with patch.object(self.tasks,'extract_catalog',return_value=CatalogBundle(entities=[])):
            catalog = self.tasks.build_document_catalog.run(self.doc,publish=False)
        with self.connect() as conn:
            batch = conn.execute("INSERT INTO ingestion_batches (knowledge_base_id,status) VALUES (%s,'processing') RETURNING id",(self.kb,)).fetchone()['id']
            conn.execute("INSERT INTO ingestion_batch_items (batch_id,position,filename,document_id,status) VALUES (%s,0,'test.pdf',%s,'extracting')",(batch,self.doc))
        bad = ClaimBundle.model_validate({'claims':[claim(),claim('Imaginary protein')]})
        def model(**kwargs):
            if kwargs.get('feedback'):
                raise self.tasks.ModelOutputError('controlled failed correction','{}')
            return bad
        with patch.object(self.tasks,'extract_claims',side_effect=model):
            result = self.tasks.extract_chunk.run(chunk,catalog['catalog_version'],0,refresh=False)
        self.assertEqual(result['status'],'partial')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM documents WHERE id=%s',(self.doc,)).fetchone()['status'],'partial')
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE chunk_id=%s',(chunk,)).fetchone()['n'],1)
            self.assertGreater(conn.execute("SELECT count(*) AS n FROM review_items WHERE document_id=%s AND kind='claim_rejection'",(self.doc,)).fetchone()['n'],0)
            self.assertEqual(conn.execute('SELECT status FROM ingestion_batches WHERE id=%s',(batch,)).fetchone()['status'],'partial')
        good = ClaimBundle(claims=[Claim.model_validate(claim()), Claim.model_validate(claim('AKT1',predicate='BINDS_TO',evidence={'sentence':'Probe Q binds AKT1.'}))])
        with patch.object(self.tasks,'extract_claims',return_value=good):
            retried = self.tasks.maintain_knowledge_base.run(self.maintenance_job())
        self.assertEqual(retried['status'],'done',str(retried))
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE chunk_id=%s',(chunk,)).fetchone()['n'],2)
            self.assertEqual(conn.execute('SELECT status FROM ingestion_batches WHERE id=%s',(batch,)).fetchone()['status'],'done')

    def test_stale_legacy_lock_and_new_heartbeat_protect_live_work(self):
        from app.recovery import reconcile_stalled
        chunk = self.chunk(self.doc,'Probe Q reduces IL-6.')
        with self.connect() as conn:
            conn.execute("UPDATE documents SET status='extracting',created_at=now()-interval '2 hours' WHERE id=%s",(self.doc,))
            conn.execute("UPDATE chunks SET updated_at=now()-interval '2 hours' WHERE id=%s",(chunk,))
        with patch('app.recovery.broker_is_idle',return_value=True):
            with self.tasks.processing_lock(f'litkg-chunk:{chunk}') as acquired:
                self.assertTrue(acquired)
                with self.connect() as conn:
                    self.assertEqual(reconcile_stalled(conn.cursor(),document_id=self.doc),[])
                    self.assertEqual(conn.execute('SELECT count(*) AS n FROM processing_leases').fetchone()['n'],1)
            # Reproduce an older worker that holds only the advisory lock.
            with self.connect() as legacy:
                legacy.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(f'litkg-chunk:{chunk}',))
                try:
                    with self.connect() as conn:
                        self.assertEqual(reconcile_stalled(conn.cursor(),document_id=self.doc),[])
                finally:
                    legacy.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(f'litkg-chunk:{chunk}',))
            with self.connect() as conn:
                self.assertEqual(reconcile_stalled(conn.cursor(),document_id=self.doc),[self.doc])
                self.assertEqual(conn.execute('SELECT status FROM chunks WHERE id=%s',(chunk,)).fetchone()['status'],'failed')
                self.assertEqual(conn.execute('SELECT status FROM documents WHERE id=%s',(self.doc,)).fetchone()['status'],'failed')

    def test_maintenance_drains_chunks_after_failure_and_finalizes_all_shared_batches(self):
        texts = [f'At {hour} h, Probe Q reduces IL-6.' for hour in [6,12,24]]
        chunks = [self.chunk(self.doc,text,i) for i,text in enumerate(texts)]
        with patch.object(self.tasks,'extract_catalog',return_value=CatalogBundle(entities=[])):
            catalog = self.tasks.build_document_catalog.run(self.doc,publish=False)
        # Reproduce the reported orphan: one failed chunk, remaining queued,
        # document extracting, no Celery tasks scheduled by this sync path.
        with self.connect() as conn:
            conn.execute("UPDATE chunks SET status='failed',error='previous interrupted maintenance' WHERE id=%s",(chunks[0],))
            batches = []
            for kb in [self.kb,self.new_kb()]:
                conn.execute('INSERT INTO knowledge_base_documents (knowledge_base_id,document_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',(kb,self.doc))
                batch = conn.execute("INSERT INTO ingestion_batches (knowledge_base_id,status) VALUES (%s,'processing') RETURNING id",(kb,)).fetchone()['id']
                conn.execute("INSERT INTO ingestion_batch_items (batch_id,position,filename,document_id,status) VALUES (%s,0,'test.pdf',%s,'extracting')",(batch,self.doc))
                batches.append(batch)
        def valid_claim(section_title,chunk_text,directory):
            return ClaimBundle.model_validate({'claims':[{'subject':{'text':'Probe Q','type':'Drug'},'predicate':'REDUCES',
                'object':{'text':'IL-6','type':'Protein'},'evidence':{'sentence':chunk_text},'confidence':.9}]})
        def fail_first(**kwargs):
            if kwargs['chunk_text'] == texts[0]:
                raise ValueError('controlled first chunk failure')
            return valid_claim(**kwargs)
        with patch.object(self.tasks,'extract_catalog',side_effect=AssertionError('Must reuse completed directory')), patch.object(self.tasks,'extract_claims',side_effect=fail_first) as model, patch.object(self.tasks.extract_chunk,'apply_async') as publish:
            first = self.tasks.maintain_knowledge_base.run(self.maintenance_job())
        self.assertEqual(model.call_count,3)
        publish.assert_not_called()
        self.assertEqual(first['status'],'partial')
        self.assertEqual([c['status'] for c in first['documents'][0]['result']['chunks']],['failed','done','done'])
        with self.connect() as conn:
            before = conn.execute('SELECT catalog_version,extraction_epoch,pipeline_generation,status FROM documents WHERE id=%s',(self.doc,)).fetchone()
            self.assertEqual(before['status'],'partial')
            self.assertEqual(conn.execute('SELECT status FROM chunks WHERE document_id=%s ORDER BY chunk_index',(self.doc,)).fetchall(),[{'status':'failed'},{'status':'done'},{'status':'done'}])
            self.assertEqual({row['status'] for row in conn.execute('SELECT status FROM ingestion_batches WHERE id=ANY(%s)',(batches,)).fetchall()},{'partial'})
            preserved = conn.execute('SELECT id FROM claims WHERE document_id=%s ORDER BY id',(self.doc,)).fetchall()
        with patch.object(self.tasks,'extract_catalog',side_effect=AssertionError('Must reuse completed directory')), patch.object(self.tasks,'extract_claims',side_effect=valid_claim) as retry_model:
            second = self.tasks.maintain_knowledge_base.run(self.maintenance_job())
        retry_model.assert_called_once()
        self.assertEqual(second['status'],'done',str(second))
        with self.connect() as conn:
            after = conn.execute('SELECT catalog_version,extraction_epoch,pipeline_generation,status FROM documents WHERE id=%s',(self.doc,)).fetchone()
            self.assertEqual(after,{**before,'status':'done'})
            self.assertEqual(after['catalog_version'],catalog['catalog_version'])
            self.assertEqual({row['status'] for row in conn.execute('SELECT status FROM ingestion_batches WHERE id=ANY(%s)',(batches,)).fetchall()},{'done'})
            current_ids = conn.execute('SELECT id FROM claims WHERE document_id=%s ORDER BY id',(self.doc,)).fetchall()
            self.assertTrue(all(row in current_ids for row in preserved))

    def test_maintenance_does_not_finalize_a_chunk_owned_by_a_concurrent_worker(self):
        chunks = [self.chunk(self.doc,f'Protein {i} was measured.',i) for i in range(2)]
        with patch.object(self.tasks,'extract_catalog',return_value=CatalogBundle(entities=[])):
            self.tasks.build_document_catalog.run(self.doc,publish=False)
        with self.tasks.processing_lock(f'litkg-chunk:{chunks[0]}') as locked:
            self.assertTrue(locked)
            with patch.object(self.tasks,'extract_claims',return_value=ClaimBundle(claims=[])) as model:
                first = self.tasks.maintain_knowledge_base.run(self.maintenance_job())
            model.assert_called_once()
        self.assertEqual(first['status'],'partial')
        self.assertEqual([c['status'] for c in first['documents'][0]['result']['chunks']],['busy','done'])
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM documents WHERE id=%s',(self.doc,)).fetchone()['status'],'extracting')
        with patch.object(self.tasks,'extract_claims',return_value=ClaimBundle(claims=[])) as retry:
            self.assertEqual(self.tasks.maintain_knowledge_base.run(self.maintenance_job())['status'],'done')
        retry.assert_called_once()

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
