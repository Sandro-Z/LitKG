"""Audit history/rollback checks require an explicitly disposable PostgreSQL DSN."""
import os
import unittest
from unittest.mock import patch
from psycopg.types.json import Jsonb

from app.claim_audit import make_audit_plan
from app.graph_query import fetch_graph
from app.review_repair import load_scope, apply_plan, rollback_run
import test_review_integration as review_tests
from test_review_regressions import candidate


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'), 'Requires disposable PostgreSQL database')
class ClaimGuardIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(review_tests.ReviewIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(review_tests.ReviewIntegrationTests.tearDownClass.__func__)
    connect = classmethod(review_tests.ReviewIntegrationTests.connect.__func__)
    seed = review_tests.ReviewIntegrationTests.seed

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
