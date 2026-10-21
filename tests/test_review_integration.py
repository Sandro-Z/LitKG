"""Optional database/API regression tests in an isolated, disposable schema."""
import json
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

from app.claim_export import fetch_claim_page
from app.claims import prepare_claims
from app.graph_query import fetch_graph
from app.graph_store import store_claim
from app.review_repair import load_scope, make_plan, apply_plan, rollback_run
from app.schemas import ClaimBundle
from test_api import TEST_ENV
from test_review_regressions import candidate, entity


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'), 'Set LITKG_TEST_DATABASE_URL to a disposable PostgreSQL database')
class ReviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, TEST_ENV):
            from app import main
        cls.main = main
        cls.client = TestClient(main.app)
        cls.dsn = os.environ['LITKG_TEST_DATABASE_URL']
        cls.schema = 'litkg_review_' + uuid.uuid4().hex
        with psycopg.connect(cls.dsn) as conn:
            conn.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
            conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        with cls.connect() as conn:
            conn.execute((Path(__file__).resolve().parents[1]/'db/init.sql').read_text())

    @classmethod
    def connect(cls):
        return psycopg.connect(cls.dsn, row_factory=dict_row, options=f'-csearch_path={cls.schema},public')

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def seed(self, text='FMT increased CD8 cells.', count=1, raw=None):
        marker = uuid.uuid4().hex
        with self.connect() as conn:
            kb = conn.execute('INSERT INTO knowledge_bases(slug,name) VALUES (%s,%s) RETURNING id',(marker,marker)).fetchone()['id']
            doc = conn.execute("INSERT INTO documents(sha256,filename,source_path,status,catalog_status,catalog_version) VALUES (%s,'4441022a.pdf','/test/4441022a.pdf','done','done','v') RETURNING id",(marker,)).fetchone()['id']
            conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES (%s,%s)',(kb,doc))
            chunk = conn.execute("INSERT INTO chunks(document_id,chunk_index,text,status,page_start,page_end,page_spans) VALUES (%s,0,%s,'done',3,3,%s) RETURNING id",
                (doc,text,Jsonb([{'start':0,'end':len(text),'page_start':3,'page_end':3}]))).fetchone()['id']
            if count > 1:
                for i in range(count):
                    conn.execute("INSERT INTO claims(document_id,chunk_id,evidence_text,claim_json,catalog_version,processing_version,extraction_model) VALUES (%s,%s,%s,%s,'v','fixed-version','fixed-model')",
                        (doc,chunk,text,Jsonb(raw or candidate(text))))
            else:
                with conn.cursor() as cur:
                    bundle = prepare_claims(ClaimBundle(claims=[raw or candidate(text)]), [], {'id':chunk,'text':text}, 'v')
                    self.assertFalse(bundle._claim_rejections)
                    store_claim(cur,document_id=doc,chunk_id=chunk,claim=bundle.claims[0],extraction_model='fixed-model')
        return kb,doc,chunk

    def test_export_api_over_500_preserves_nested_fields_ids_versions_and_counts(self):
        raw = candidate('FMT increased CD8 cells.', participants=[{'role':'co_intervention','entity':entity('anti-PD-1','Drug')}],
            qualifiers={'dose':{'value':5,'unit':'μg'}}, source_kind='primary')
        kb,doc,chunk = self.seed(count=1107,raw=raw)
        with patch.object(self.main,'get_conn',side_effect=self.connect):
            page = self.client.get(f'/knowledge-bases/{kb}/claims?limit=500&offset=1000').json()
            exported = self.client.get(f'/knowledge-bases/{kb}/claims/export').json()
            csv = self.client.get(f'/knowledge-bases/{kb}/claims/export?format=csv')
        self.assertEqual(page['total'],1107)
        self.assertEqual(len(page['claims']),107)
        self.assertEqual((exported['exported_count'],exported['total']),(1107,1107))
        ids = [c['id'] for c in exported['claims']]
        self.assertEqual(len(set(ids)),1107)
        self.assertEqual(ids,sorted(ids))
        self.assertEqual(exported['claims'][0]['claim_json'], raw)
        self.assertEqual(exported['claims'][0]['participants'],raw['participants'])
        self.assertEqual(exported['claims'][0]['process']['object'],raw['object']['process'])
        self.assertEqual(exported['claims'][0]['processing_version'],'fixed-version')
        self.assertEqual(csv.headers['x-exported-count'],'1107')
        self.assertIn('μg',csv.content.decode('utf-8-sig'))

    def test_negative_storage_filters_display_and_pending_graph_are_consistent(self):
        for sentence,status,direction in [('FMT did not affect CD8 cells.','no_effect_reported','unchanged'),
                                           ('CD8 cells were unchanged after FMT.','no_effect_reported','unchanged'),
                                           ('FMT did not significantly affect CD8 cells.','not_significant','unspecified')]:
            raw = candidate(sentence,negated=True)
            raw['object']['process']['direction']='unspecified'
            kb,doc,_ = self.seed(sentence,raw=raw)
            with self.connect() as conn:
                row = conn.execute('SELECT * FROM claims WHERE document_id=%s',(doc,)).fetchone()
                self.assertFalse(row['negated'])
                self.assertEqual(row['claim_json']['object']['process']['direction'],direction)
                with conn.cursor() as cur:
                    self.assertEqual(fetch_claim_page(cur,knowledge_base_id=kb,result_status=status)['total'],1)
                    graph = fetch_graph(cur,knowledge_base_id=kb,result_status=status,include_total=True)
                    self.assertEqual(graph['total'],1)
                    self.assertEqual(graph['edges'][0]['result_status'],status)
        raw = candidate('Disease progression occurred after FMT.',object_=entity('Disease progression','ClinicalOutcome'),predicate='CAUSES')
        kb,doc,_ = self.seed(raw['evidence']['sentence'],raw=raw)
        with self.connect() as conn:
            with conn.cursor() as cur:
                self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True)['total'],0)
                pending = fetch_graph(cur,knowledge_base_id=kb,include_total=True,include_pending=True)
                self.assertEqual(pending['edges'][0]['review_status'],'pending')
            self.assertTrue(conn.execute("SELECT id FROM review_items WHERE document_id=%s AND status='pending'",(doc,)).fetchall())

    def test_apply_repair_and_rollback_restore_claim_ids_raw_json_and_projection(self):
        kb,doc,_ = self.seed()
        with self.connect() as conn:
            with conn.cursor() as cur:
                document,chunks,claims = load_scope(cur,document_id=doc)
                plan = make_plan(document,chunks,claims,[],mode='repair')
        # Force a visible legacy encoding repair rather than changing already-normalized rows.
        with self.connect() as conn:
            raw = candidate('FMT increased CD8 cells.')
            conn.execute('UPDATE claims SET claim_json=%s WHERE document_id=%s',(Jsonb(raw),doc))
            with conn.cursor() as cur:
                document,chunks,claims = load_scope(cur,document_id=doc)
                before = json.loads(json.dumps(claims,default=str))
                before_graph = fetch_graph(cur,knowledge_base_id=kb,include_total=True)
                plan = make_plan(document,chunks,claims,[],mode='repair')
        self.assertTrue(plan['changes'])
        with patch('app.db.get_conn',side_effect=self.connect):
            applied = apply_plan(plan)
            self.assertEqual(applied['status'],'applied')
            preview = rollback_run(plan['run_id'])
            self.assertEqual(preview['status'],'rollback_preview')
            rollback_run(plan['run_id'],apply=True)
        with self.connect() as conn:
            after = json.loads(json.dumps(conn.execute('SELECT * FROM claims WHERE document_id=%s ORDER BY id',(doc,)).fetchall(),default=str))
            self.assertEqual(after,before)
            with conn.cursor() as cur:
                self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True),before_graph)

    def test_source_correction_reextract_diff_apply_and_rollback_without_live_model(self):
        source = 'FMT increased CD8 cells at 5 mg. Genotype 1/1. Abstract: neighboring article.'
        kb,doc,chunk = self.seed(source)
        with self.connect() as conn:
            with conn.cursor() as cur:
                document,chunks,claims = load_scope(cur,document_id=doc)
        edits=[]
        for old,new in [('5 mg','5 μg'),('1/1','+/+'),('Abstract: neighboring article.','')]:
            start=source.index(old)
            edits.append({'chunk_id':chunk,'start':start,'end':start+len(old),'original':old,'replacement':new,
                          'page':3,'basis':'Fixed PDF region supplied in regression fixture'})
        corrected='FMT increased CD8 cells at 5 μg. Genotype +/+. '
        model=lambda *args,**kwargs: ClaimBundle(claims=[candidate('FMT increased CD8 cells at 5 μg.')])
        plan=make_plan(document,chunks,claims,[],mode='reextract',corrections=edits,extractor=model)
        self.assertTrue(plan['applicable'])
        with patch('app.db.get_conn',side_effect=self.connect):
            apply_plan(plan)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT text FROM chunks WHERE id=%s',(chunk,)).fetchone()['text'],corrected)
                saved=conn.execute('SELECT record FROM scoped_repair_runs WHERE id=%s::UUID',(plan['run_id'],)).fetchone()['record']
                self.assertEqual(saved['plan']['corrections'],edits)
            rollback_run(plan['run_id'],apply=True)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT text FROM chunks WHERE id=%s',(chunk,)).fetchone()['text'],source)

    def test_stale_plan_cannot_overwrite_newer_work(self):
        _,doc,chunk=self.seed()
        with self.connect() as conn:
            with conn.cursor() as cur:
                document,chunks,claims=load_scope(cur,document_id=doc)
                plan=make_plan(document,chunks,claims,[],mode='repair')
            conn.execute("UPDATE chunks SET text=text || ' newer' WHERE id=%s",(chunk,))
        with patch('app.db.get_conn',side_effect=self.connect):
            with self.assertRaisesRegex(ValueError,'changed since preview'):
                apply_plan(plan)

    def test_legacy_negative_affects_keeps_raw_polarity_and_consistent_display_filter(self):
        raw=candidate('FMT did not affect CD8 cells.',negated=True)
        raw['object']['process']['direction']='unchanged'
        kb,doc,_=self.seed(raw['evidence']['sentence'],raw=raw)
        with self.connect() as conn:
            # Simulate a pre-upgrade projection, including its original negation.
            conn.execute('UPDATE claims SET negated=true,claim_json=%s WHERE document_id=%s',(Jsonb(raw),doc))
            conn.execute('UPDATE relations SET negated=true,qualifiers=%s WHERE id IN (SELECT relation_id FROM facts WHERE claim_id IN (SELECT id FROM claims WHERE document_id=%s))',
                (Jsonb({'processes':[{'role':'object','kind':'abundance','direction':'unchanged'}]}),doc))
            with conn.cursor() as cur:
                page=fetch_claim_page(cur,knowledge_base_id=kb,result_status='no_effect_reported')
                self.assertTrue(page['claims'][0]['negated'])
                self.assertFalse(page['claims'][0]['display_negated'])
                graph=fetch_graph(cur,knowledge_base_id=kb,include_negated=False,include_total=True)
                self.assertEqual(graph['total'],1)
                self.assertTrue(graph['edges'][0]['negated'])
                self.assertFalse(graph['edges'][0]['display_negated'])

    def test_complete_facts_export_over_500_retains_real_ids_roles_and_both_evidence_quotes(self):
        source='FMT with anti-PD-1 increased CD8 cells. Evidence from donors was measured.'
        raw=candidate('FMT with anti-PD-1 increased CD8 cells.',participants=[
            {'role':'co_intervention','entity':entity('anti-PD-1','Drug')}],
            additional_evidence=[{'sentence':'Evidence from donors was measured.'}])
        kb,doc,chunk=self.seed(source,raw=raw)
        with self.connect() as conn:
            template=conn.execute('SELECT * FROM facts WHERE claim_id IN (SELECT id FROM claims WHERE document_id=%s)',(doc,)).fetchone()
            # Fixed persisted data: duplicate source assertions with unique IDs,
            # keeping fact participants and all evidence without model calls.
            for index in range(600):
                cid=conn.execute("INSERT INTO claims(document_id,chunk_id,evidence_text,claim_json,catalog_version,extraction_model) VALUES (%s,%s,%s,%s,'v','fixed-model') RETURNING id",
                    (doc,chunk,raw['evidence']['sentence'],Jsonb(raw))).fetchone()['id']
                fid=conn.execute("INSERT INTO facts(claim_id,fact_key,predicate,qualifiers,negated,speculative,catalog_version,processing_version) VALUES (%s,%s,'AFFECTS',%s,false,false,'v','test-version') RETURNING id",
                    (cid,str(index),Jsonb(template['qualifiers']))).fetchone()['id']
                conn.execute('INSERT INTO fact_participants SELECT %s,entity_id,role,mention FROM fact_participants WHERE fact_id=%s',(fid,template['id']))
                conn.execute('INSERT INTO fact_evidence SELECT %s,ordinal,evidence FROM fact_evidence WHERE fact_id=%s',(fid,template['id']))
        with patch.object(self.main,'get_conn',side_effect=self.connect):
            response=self.client.get(f'/knowledge-bases/{kb}/facts/export')
            csv=self.client.get(f'/knowledge-bases/{kb}/facts/export?format=csv')
            claims=self.client.get(f'/knowledge-bases/{kb}/claims/export').json()
            scoped=self.client.get(f'/knowledge-bases/{kb}/facts/export?document_id={doc+10000}').json()
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual((result['exported_count'],result['total'],result['document_count']),(601,601,1))
        self.assertEqual(len({f['id'] for f in result['facts']}),601)
        self.assertEqual({f['claim_id'] for f in result['facts']},{c['id'] for c in claims['claims']})
        self.assertEqual({c['fact_id'] for c in claims['claims']},{f['id'] for f in result['facts']})
        self.assertEqual(csv.headers['x-exported-count'],'601')
        self.assertEqual(scoped['total'],0)
        for fact in result['facts']:
            self.assertEqual(len(fact['evidence']),2)
            self.assertTrue(all(p['entity_id'] for p in fact['participants']))
            self.assertIn('co_intervention',{p['role'] for p in fact['participants']})
            self.assertEqual(fact['qualifiers']['processes'],template['qualifiers']['processes'])
            self.assertIn('reviews',fact)
            self.assertNotIn('source_text',fact)

    def test_document_graph_filter_remains_within_knowledge_base(self):
        kb,doc,_=self.seed()
        other_kb,other_doc,_=self.seed()
        with patch.object(self.main,'get_conn',side_effect=self.connect):
            visible=self.client.get(f'/knowledge-bases/{kb}/graph?document_id={doc}').json()
            invisible=self.client.get(f'/knowledge-bases/{kb}/graph?document_id={other_doc}').json()
        self.assertEqual(visible['total'],1)
        self.assertEqual(invisible['total'],0)


if __name__=='__main__':
    unittest.main()
