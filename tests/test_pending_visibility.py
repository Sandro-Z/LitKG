import unittest
from collections import defaultdict
from unittest.mock import patch

from app.graph_query import fetch_graph
from app.schemas import QuestionRequest
from app import retrieval


class Cursor:
    def __init__(self):
        self.executions = []
    def execute(self, query, params):
        self.executions.append((query, params))
    def fetchall(self):
        return [{'source_id': 1}] if len(self.executions) == 2 else []


class PendingVisibilityTests(unittest.TestCase):
    def fact(self):
        return defaultdict(str, {
            'id': 1, 'claim_id': 10, 'document_id': 2, 'chunk_id': 3,
            'filename': 'paper.pdf', 'title': 'Example', 'doi': None,
            'predicate': 'PRODUCES', 'participants': [], 'qualifiers': {},
            'negated': False, 'speculative': False, 'audit_pending': True,
            'claim_json': {'semantic_status': 'pending', 'review_reasons': ['source unclassified']},
            'reviews': [{'status': 'pending'}],
            'evidence': [{'sentence': 'Bacteroides produces acetate.'}],
        })

    def test_question_defaults_to_visible_and_supports_strict_override(self):
        self.assertTrue(QuestionRequest(question='acetate').include_pending)
        self.assertFalse(QuestionRequest(question='acetate', include_pending=False).include_pending)

    def test_graph_defaults_to_pending_visible_and_strict_query_keeps_filter(self):
        visible = Cursor()
        strict = Cursor()
        fetch_graph(visible, knowledge_base_id=30)
        fetch_graph(strict, knowledge_base_id=30, include_pending=False)
        pattern = r"COALESCE\s*\(c.claim_json\s*->>\s*'semantic_status'\s*,\s*'[^']*'\s*\)\s*(?:<>|!=)\s*'pending'"
        self.assertNotRegex(visible.executions[0][0], pattern)
        self.assertRegex(strict.executions[0][0], pattern)

    def test_pending_retrieval_keeps_state_in_fact_and_citation(self):
        with patch.object(retrieval, 'embedding_config', return_value=None), patch.object(retrieval, 'fetch_facts', return_value=[self.fact()]):
            result = retrieval.retrieve(Cursor(), 30, 'acetate', hops=0)
        self.assertEqual(len(result['facts']), 1)
        self.assertEqual(result['facts'][0]['semantic_status'], 'pending')
        self.assertTrue(result['facts'][0]['audit_pending'])
        self.assertEqual(result['citations'][0]['review_status'], 'pending')
        self.assertTrue(result['warnings'])

    def test_strict_retrieval_excludes_pending(self):
        with patch.object(retrieval, 'embedding_config', return_value=None), patch.object(retrieval, 'fetch_facts', return_value=[self.fact()]):
            result = retrieval.retrieve(Cursor(), 30, 'acetate', hops=0, include_pending=False)
        self.assertEqual(result['facts'], [])
        self.assertEqual(result['citations'], [])

    def test_expansion_filter_is_applied_only_in_strict_mode(self):
        for include_pending in [True, False]:
            cursor = Cursor()
            with patch.object(retrieval, 'embedding_config', return_value=None), patch.object(retrieval, 'fetch_facts', return_value=[]):
                retrieval.retrieve(cursor, 30, 'acetate', hops=1, include_pending=include_pending)
            expansion = cursor.executions[-1][0]
            self.assertEqual("COALESCE(c.claim_json->>'semantic_status'" in expansion, not include_pending)


if __name__ == '__main__':
    unittest.main()
