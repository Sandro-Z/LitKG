"""Audit history/rollback checks require an explicitly disposable PostgreSQL DSN."""
import os
import unittest
from unittest.mock import patch
from psycopg.types.json import Jsonb

from app.claim_audit import make_audit_plan
from app.graph_query import fetch_graph
from app.claim_semantics import result_sql, result_view
from app.review_repair import load_scope, apply_plan, rollback_run
import test_review_integration as review_tests
from test_review_regressions import candidate


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'), 'Requires disposable PostgreSQL database')
class ClaimGuardIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(review_tests.ReviewIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(review_tests.ReviewIntegrationTests.tearDownClass.__func__)
    connect = classmethod(review_tests.ReviewIntegrationTests.connect.__func__)
    seed = review_tests.ReviewIntegrationTests.seed

    def test_sql_and_python_agree_for_speculative_legacy_fields(self):
        cases=[{'predicate':'AFFECTS','speculative':True,'result_status':'observed_change'},
               {'predicate':'AFFECTS','speculative':True,'object':{'process':{'direction':'increased'}}},
               {'predicate':'AFFECTS','speculative':True,'object':{'process':{'direction':'unchanged'}}},
               {'predicate':'AFFECTS','speculative':True,'result_status':'not_significant'},
               {'claim_json':{'predicate':'AFFECTS','speculative':False,'result_status':'observed_change'},'predicate':'AFFECTS','speculative':True}]
        with self.connect() as conn:
            for value in cases:
                raw=value.get('claim_json') or value
                row=conn.execute(f'WITH c AS (SELECT %s::jsonb AS claim_json,%s::TEXT AS predicate,%s::BOOLEAN AS negated,%s::BOOLEAN AS speculative) SELECT {result_sql("c")} AS status FROM c',
                                 (Jsonb(raw),value['predicate'],value.get('negated',False),value.get('speculative',False))).fetchone()
                self.assertEqual(row['status'],result_view(value))

    def test_recheck_is_deduplicated_preserves_review_decision_and_clears_fixed_claim(self):
        sentence='No relationship between FMT and CD8 cells has yet been identified.'
        raw=candidate(sentence,predicate='CORRELATED_WITH')
        kb,doc,chunk=self.seed(sentence,raw=raw)
        with self.connect() as conn:
            conn.execute('UPDATE claims SET claim_json=%s WHERE document_id=%s',(Jsonb(raw),doc))
            document,chunks,claims=load_scope(conn.cursor(),document_id=doc)
        with patch('app.db.get_conn',side_effect=self.connect):
            apply_plan(make_audit_plan(document,chunks,claims))
            with self.connect() as conn:
                conn.execute("UPDATE review_items SET status='dismissed' WHERE document_id=%s AND kind='claim_audit'",(doc,))
                document,chunks,claims=load_scope(conn.cursor(),document_id=doc)
            apply_plan(make_audit_plan(document,chunks,claims,active_claim_ids={claims[0]['id']}))
            with self.connect() as conn:
                reviews=conn.execute("SELECT status FROM review_items WHERE document_id=%s AND kind='claim_audit'",(doc,)).fetchall()
                self.assertEqual(reviews,[{'status':'dismissed'}])
                self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True)['total'],0)
                corrected=candidate('FMT is correlated with CD8 cells.',predicate='CORRELATED_WITH')
                conn.execute('UPDATE claims SET claim_json=%s,evidence_text=%s WHERE id=%s',(Jsonb(corrected),corrected['evidence']['sentence'],claims[0]['id']))
                conn.execute('UPDATE chunks SET text=%s WHERE id=%s',(corrected['evidence']['sentence'],chunk))
                document,chunks,claims=load_scope(conn.cursor(),document_id=doc)
            plan=make_audit_plan(document,chunks,claims,active_claim_ids={claims[0]['id']})
            self.assertFalse(plan['reviews'][0]['active'])
            apply_plan(plan)
        with self.connect() as conn:
            saved=conn.execute('SELECT claim_json FROM claims WHERE id=%s',(claims[0]['id'],)).fetchone()
            self.assertEqual(saved['claim_json'],corrected)
            self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True)['total'],1)

    def test_audit_keeps_raw_claim_and_id_and_can_rollback_reviews(self):
        sentence = 'No relationship between FMT and CD8 cells has yet been identified.'
        raw = candidate(sentence, predicate='CORRELATED_WITH')
        kb, doc, _ = self.seed(sentence, raw=raw)
        with self.connect() as conn:
            conn.execute('UPDATE claims SET claim_json=%s WHERE document_id=%s', (Jsonb(raw),doc))
            cur = conn.cursor()
            document,chunks,claims = load_scope(cur,document_id=doc)
            self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True)['total'],1)
            plan = make_audit_plan(document,chunks,claims)
            self.assertEqual(plan['changes'],[])
            self.assertTrue(plan['reviews'])
        with patch('app.db.get_conn',side_effect=self.connect):
            applied = apply_plan(plan)
            self.assertEqual(applied['written_claim_ids'],[])
            with self.connect() as conn:
                cur = conn.cursor()
                saved = conn.execute('SELECT * FROM claims WHERE document_id=%s',(doc,)).fetchone()
                self.assertEqual((saved['id'],saved['claim_json']), (claims[0]['id'],raw))
                self.assertTrue(conn.execute('SELECT id FROM claim_history WHERE original_claim_id=%s',(saved['id'],)).fetchone())
                self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True)['total'],0)
                self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True,include_pending=True)['total'],1)
            self.assertEqual(rollback_run(applied['run_id'])['status'],'rollback_preview')
            rollback_run(applied['run_id'],apply=True)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT claim_json FROM claims WHERE id=%s',(claims[0]['id'],)).fetchone()['claim_json'],raw)
            self.assertEqual(fetch_graph(conn.cursor(),knowledge_base_id=kb,include_total=True)['total'],1)
