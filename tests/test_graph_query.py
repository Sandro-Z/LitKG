import unittest

from app.graph_query import fetch_graph


class FakeCursor:
    def __init__(self, responses):
        self.responses = responses
        self.executions = []
        self.current = None

    def execute(self, query, params):
        self.executions.append((query, params))
        self.current = self.responses[len(self.executions) - 1]

    def fetchall(self):
        return self.current

    def fetchone(self):
        return self.current


class GraphQueryTests(unittest.TestCase):
    def test_builds_aggregated_graph_with_provenance(self):
        relation = {
            "id": 7,
            "subject_entity_id": 10,
            "subject_name": "Aspirin",
            "subject_type": "Drug",
            "subject_normalized_id": "CHEBI:15365",
            "predicate": "INHIBITS",
            "object_entity_id": 20,
            "object_name": "COX-1",
            "object_type": "Protein",
            "object_normalized_id": None,
            "qualifiers": {"assay": "in vitro"},
            "negated": False,
            "speculative": False,
            "evidence_count": 3,
            "document_count": 2,
            "confidence": 0.9,
            "max_confidence": 0.95,
        }
        evidence = {
            "relation_id": 7,
            "claim_id": 99,
            "document_id": 5,
            "filename": "paper.pdf",
            "section_title": "Results",
            "evidence_text": "Aspirin inhibited COX-1.",
            "confidence": 0.95,
        }
        cursor = FakeCursor(
            [
                [relation],
                {"count": 2},
                [evidence],
            ]
        )

        graph = fetch_graph(
            cursor,
            knowledge_base_id=3,
            limit=100,
            evidence_limit=2,
        )

        self.assertEqual(graph["summary"]["node_count"], 2)
        self.assertEqual(graph["summary"]["relation_count"], 1)
        self.assertEqual(
            graph["summary"]["represented_document_count"],
            2,
        )
        self.assertEqual(graph["edges"][0]["document_count"], 2)
        self.assertEqual(
            graph["edges"][0]["evidence_samples"][0]["document_id"],
            5,
        )
        self.assertEqual(len(cursor.executions), 3)

    def test_empty_projection_returns_empty_graph(self):
        cursor = FakeCursor([[]])

        graph = fetch_graph(cursor, document_id=1)

        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(len(cursor.executions), 1)


if __name__ == "__main__":
    unittest.main()
