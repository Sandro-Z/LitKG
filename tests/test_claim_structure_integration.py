import copy
import os
import unittest
import uuid
from unittest.mock import patch
from psycopg.types.json import Jsonb

from app.claim_audit import make_audit_plan
from app.claim_export import fetch_claim_page
from app.claim_semantics import result_view,result_sql
from app.graph_store import store_claim,upsert_entity
from app.graph_query import fetch_graph
from app.normalization import entity_identity,normalize_alias
from app.review_repair import load_scope,apply_plan,rollback_run
from app.schemas import Claim
import test_review_integration as review_tests
from test_review_regressions import candidate,entity


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'),'Requires disposable PostgreSQL database')
class ClaimStructureIntegrationTests(unittest.TestCase):
    setUpClass=classmethod(review_tests.ReviewIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(review_tests.ReviewIntegrationTests.tearDownClass.__func__)
    connect=classmethod(review_tests.ReviewIntegrationTests.connect.__func__)
    seed=review_tests.ReviewIntegrationTests.seed

    def test_method_audit_preserves_raw_and_uses_server_section_and_rollback(self):
        raw=candidate('FMT increased CD8 cells.',result_status='observed_change')
        kb,doc,chunk=self.seed(raw['evidence']['sentence'],raw=raw)
        with self.connect() as conn:
            conn.execute("UPDATE chunks SET section_title='Materials and Methods' WHERE id=%s",(chunk,))
            document,chunks,claims=load_scope(conn.cursor(),document_id=doc)
            original=copy.deepcopy(claims[0]['claim_json'])
            plan=make_audit_plan(document,chunks,claims)
            self.assertTrue(any(f['rule_id']=='V6-METHOD-RESULT' for r in plan['reviews'] for f in r['findings']))
        with patch('app.db.get_conn',side_effect=self.connect):
            applied=apply_plan(plan)
            with self.connect() as conn:
                saved=conn.execute('SELECT id,claim_json FROM claims WHERE document_id=%s',(doc,)).fetchone()
                self.assertEqual((saved['id'],saved['claim_json']),(claims[0]['id'],original))
                self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True)['total'],0)
            rollback_run(applied['run_id'],apply=True)
        with self.connect() as conn:
            self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True)['total'],1)

    def test_projection_detects_different_names_sharing_actual_id(self):
        kb,doc,chunk=self.seed()
        raw=candidate('Aspirin affects acetylsalicylic acid.',subject=entity('Aspirin','Compound'),
                      object_=entity('acetylsalicylic acid','Compound',{'kind':'activity','direction':'increased'}))
        raw['subject']['normalized_id']='CHEBI:15365';raw['object']['normalized_id']='CHEBI:15365'
        with self.connect() as conn:
            conn.execute('DELETE FROM claims WHERE document_id=%s',(doc,))
            conn.execute('UPDATE chunks SET text=%s WHERE id=%s',(raw['evidence']['sentence'],chunk))
            cid,_=store_claim(conn.cursor(),document_id=doc,chunk_id=chunk,claim=Claim.model_validate(raw),extraction_model='fixture',original_claim_json=raw)
            row=conn.execute('SELECT * FROM claims WHERE id=%s',(cid,)).fetchone()
            self.assertEqual(row['claim_json'],raw)
            self.assertEqual(row['subject_entity_id'],row['object_entity_id'])
            self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True)['total'],0)
            self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True,include_pending=True)['total'],1)
            self.assertEqual(fetch_claim_page(conn.cursor(),knowledge_base_id=kb)['claims'][0]['semantic_status'],'pending')

    def test_format_merge_that_creates_self_loop_is_audited(self):
        kb,doc,chunk=self.seed()
        raw=candidate('sodium-butyrate affects sodium_butyrate.',subject=entity('sodium-butyrate','Compound'),object_=entity('sodium_butyrate','Compound'))
        with self.connect() as conn:
            cur=conn.cursor();ids=[]
            for name in ['sodium-butyrate','sodium_butyrate']:
                eid=conn.execute('INSERT INTO entities(canonical_key,canonical_name,entity_type) VALUES (%s,%s,\'Compound\') RETURNING id',('legacy-'+uuid.uuid4().hex,name)).fetchone()['id']
                conn.execute('INSERT INTO entity_aliases(entity_id,alias,normalized_alias) VALUES (%s,%s,%s)',(eid,name,normalize_alias(name)))
                ids.append(eid)
            c=conn.execute('SELECT * FROM claims WHERE document_id=%s',(doc,)).fetchone()
            conn.execute('UPDATE claims SET subject_entity_id=%s,object_entity_id=%s,claim_json=%s WHERE id=%s',(*ids,Jsonb(raw),c['id']))
            relation=conn.execute('SELECT relation_id FROM relation_evidence WHERE claim_id=%s',(c['id'],)).fetchone()['relation_id']
            conn.execute('UPDATE relations SET subject_entity_id=%s,object_entity_id=%s WHERE id=%s',(*ids,relation))
            for role,eid in zip(['subject','object'],ids):
                conn.execute('UPDATE fact_participants SET entity_id=%s WHERE role=%s AND fact_id=(SELECT id FROM facts WHERE claim_id=%s)',(eid,role,c['id']))
            upsert_entity(cur,entity_identity('sodium butyrate','Compound'),reconcile_existing=True)
            row=conn.execute('SELECT * FROM claims WHERE id=%s',(c['id'],)).fetchone()
            self.assertEqual(row['subject_entity_id'],row['object_entity_id'])
            self.assertEqual(row['claim_json'],raw)
            self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True)['total'],0)

    def test_method_marker_python_and_sql_agree(self):
        value={'predicate':'AFFECTS','result_status':'observed_change','qualifiers':{'result_interpretation':'method_description'}}
        with self.connect() as conn:
            got=conn.execute(f'WITH c AS (SELECT %s::jsonb AS claim_json,\'AFFECTS\'::TEXT AS predicate,false AS negated) SELECT {result_sql("c")} AS status FROM c',(Jsonb(value),)).fetchone()['status']
            self.assertEqual(got,result_view(value))
            self.assertEqual(got,'unknown')
