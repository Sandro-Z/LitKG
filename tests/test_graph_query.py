import unittest

from app.graph_query import fetch_graph, fetch_relations


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
            "title": "Aspirin study",
            "doi": "10.1234/study",
            "chunk_page_start": 2,
            "chunk_page_end": 4,
            "claim_json": {"evidence": {"page": 3, "page_end": 3, "table_or_figure": "Fig. 1"}},
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
        sample = graph["edges"][0]["evidence_samples"][0]
        self.assertEqual(sample["doi"], "10.1234/study")
        self.assertEqual(sample["title"], "Aspirin study")
        self.assertEqual((sample["page"], sample["page_end"]), (3, 3))
        self.assertEqual(sample["table_or_figure"], "Fig. 1")

    def test_empty_projection_returns_empty_graph(self):
        cursor = FakeCursor([[]])

        graph = fetch_graph(cursor, document_id=1)

        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(len(cursor.executions), 1)

    def test_search_is_parameterized_and_wildcards_are_literal(self):
        cursor = FakeCursor([[]])
        query = "EGFR_%!'; SELECT 1; --"
        fetch_graph(cursor, knowledge_base_id=3, query=query, predicate="binds to", entity_type="protein")
        sql, params = cursor.executions[0]
        self.assertNotIn(query, sql)
        self.assertIn("alias.alias ILIKE", sql)
        self.assertEqual(params[1:10], ("%EGFR!_!%!!'; SELECT 1; --%",) * 9)
        self.assertEqual(params[10:13], ("BINDS_TO", "Protein", "Protein"))

    def test_direction_and_subject_object_filters(self):
        for direction, fragment in [
            ("outgoing", "r.subject_entity_id = %s"),
            ("incoming", "r.object_entity_id = %s"),
            ("both", "r.subject_entity_id = %s OR r.object_entity_id = %s"),
        ]:
            with self.subTest(direction=direction):
                cursor = FakeCursor([[]])
                fetch_graph(cursor, knowledge_base_id=3, entity_id=12, direction=direction)
                sql, params = cursor.executions[0]
                self.assertIn(fragment, sql)
                self.assertEqual(params[1:-3], (12, 12) if direction == "both" else (12,))
        cursor = FakeCursor([[]])
        fetch_graph(cursor, knowledge_base_id=3, subject="Aspirin", object_query="COX-1", subject_entity_id=10, object_entity_id=20)
        self.assertEqual(cursor.executions[0][1][1:-3], ("%Aspirin%",) * 4 + ("%COX-1%",) * 4 + (10, 20))

    def test_past_last_page_keeps_total(self):
        cursor = FakeCursor([{"total": 7}, []])
        result = fetch_relations(cursor, knowledge_base_id=3, limit=10, offset=100)
        self.assertEqual(result["relations"], [])
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["offset"], 100)
        self.assertEqual(cursor.executions[1][1][-2:], (10, 100))

    def test_direction_requires_entity_id(self):
        with self.assertRaises(ValueError):
            fetch_graph(FakeCursor([]), knowledge_base_id=3, direction="outgoing")


if __name__ == "__main__":
    unittest.main()
