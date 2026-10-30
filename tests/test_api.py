import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


TEST_ENV = {
    "DATABASE_URL": "postgresql://localhost/litkg_test",
    "REDIS_URL": "redis://localhost:6379/15",
    "LLM_BASE_URL": "http://127.0.0.1:1/v1",
    "LLM_API_KEY": "test-only",
    "LLM_MODEL": "test-only",
    "UPLOAD_DIR": os.path.join(tempfile.gettempdir(), "litkg-unit-test-uploads"),
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, TEST_ENV):
            from app import main
        cls.main = main
        cls.client = TestClient(main.app)

    def connection(self, row=None, rows=None):
        context = MagicMock()
        cursor = context.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = row
        cursor.fetchall.return_value = rows or []
        return context, cursor

    def test_shared_microbiome_vocabularies_are_available_without_database_access(self):
        with patch.object(self.main, 'get_conn', side_effect=AssertionError('Vocabulary is local')):
            types = self.client.get('/entity-types')
            predicates = self.client.get('/predicate-types')
        self.assertEqual(types.status_code, 200)
        self.assertIn('Protein', types.json()['types'])
        self.assertIn('MicrobialStrain', types.json()['types'])
        self.assertEqual(predicates.status_code, 200)
        registry = predicates.json()
        self.assertEqual(registry['type_parents']['MicrobialStrain'], 'MicrobialTaxon')
        self.assertIn('diversity', registry['process_kinds'])
        self.assertTrue(registry['predicates']['CORRELATED_WITH']['symmetric'])
        self.assertIn('metric', registry['predicates']['DIFFERS_BETWEEN']['required_qualifiers'])

    def test_both_claims_endpoints_return_document_and_page_metadata(self):
        claim = {
            "id": 99, "document_id": 5, "chunk_id": 8,
            "filename": "paper.pdf", "title": "Aspirin study", "doi": "10.1234/study",
            "section_title": "Results", "chunk_page_start": 2, "chunk_page_end": 5,
            "subject_text": "Aspirin", "predicate": "INHIBITS", "object_text": "COX-1",
            "claim_json": {"evidence": {"sentence": "Aspirin inhibited COX-1.", "page": 3, "page_end": 4}},
        }
        for path in ["/documents/5/claims", "/knowledge-bases/3/claims"]:
            with self.subTest(path=path):
                connection, _ = self.connection(row={"id": 3}, rows=[claim])
                with patch.object(self.main, "get_conn", return_value=connection):
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                result = response.json()["claims"][0]
                self.assertEqual(result["title"], "Aspirin study")
                self.assertEqual(result["doi"], "10.1234/study")
                self.assertEqual((result["page"], result["page_end"]), (3, 4))
                self.assertNotIn("chunk_page_start", result)
                self.assertEqual(result["claim_json"], claim["claim_json"])

    def test_relations_route_forwards_filters_and_object_alias(self):
        connection, _ = self.connection(row={"id": 3})
        result = {"knowledge_base_id": 3, "relations": [], "total": 0, "limit": 20, "offset": 40}
        with patch.object(self.main, "get_conn", return_value=connection), patch.object(
            self.main, "fetch_relations", return_value=result
        ) as fetch:
            response = self.client.get("/knowledge-bases/3/relations", params={
                "query": "EGFR", "object": "EGFR", "subject": "Gefitinib",
                "entity_id": 12, "direction": "incoming", "predicate": "inhibits",
                "include_negated": "false", "include_pending":"true", "limit": 20, "offset": 40,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        kwargs = fetch.call_args.kwargs
        self.assertEqual(kwargs["object_query"], "EGFR")
        self.assertEqual(kwargs["subject"], "Gefitinib")
        self.assertEqual(kwargs["entity_id"], 12)
        self.assertEqual(kwargs["direction"], "incoming")
        self.assertFalse(kwargs["include_negated"])
        self.assertTrue(kwargs['include_pending'])
        self.assertEqual((kwargs["limit"], kwargs["offset"]), (20, 40))

    def test_graph_route_supports_entity_search(self):
        connection, _ = self.connection(row={"id": 3})
        with patch.object(self.main, "get_conn", return_value=connection), patch.object(
            self.main, "fetch_graph", return_value={"nodes": [], "edges": []}
        ) as fetch:
            response = self.client.get("/knowledge-bases/3/graph", params={"query": "EGFR", "entity_id": 12, "direction": "incoming"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_args.kwargs["query"], "EGFR")
        self.assertEqual(fetch.call_args.kwargs["entity_id"], 12)

    def test_invalid_filters_are_rejected_before_database_access(self):
        invalid = [
            {"limit": 0}, {"limit": 1001}, {"offset": -1}, {"evidence_limit": 21},
            {"entity_id": 0}, {"subject_entity_id": -1}, {"object_entity_id": 0},
            {"direction": "invalid"}, {"direction": "incoming"}, {"query": "x" * 201},
        ]
        with patch.object(self.main, "get_conn") as connect:
            for params in invalid:
                with self.subTest(params=params):
                    response = self.client.get("/knowledge-bases/3/relations", params=params)
                    self.assertEqual(response.status_code, 422)
            connect.assert_not_called()

    def test_unknown_knowledge_base_and_document_return_404(self):
        for path in ["/knowledge-bases/999/relations", "/documents/999/claims"]:
            with self.subTest(path=path):
                connection, _ = self.connection()
                with patch.object(self.main, "get_conn", return_value=connection):
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_knowledge_base_update_trims_fields_and_preserves_identity(self):
        row = {"id": 3, "slug": "kb-original", "name": "新名称", "description": None}
        connection, cursor = self.connection(row=row)
        with patch.object(self.main, "get_conn", return_value=connection):
            response = self.client.put("/knowledge-bases/3", json={"name": " 新名称 ", "description": "  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), row)
        self.assertEqual(cursor.execute.call_args.args[1], ("新名称", None, 3))
        self.assertIn("updated_at = now()", cursor.execute.call_args.args[0])

    def test_knowledge_base_update_rejects_invalid_fields(self):
        with patch.object(self.main, "get_conn") as connect:
            for payload in [{"name": ""}, {"name": "   "}, {"name": "x" * 201},
                            {"name": "valid", "description": "x" * 2001}, {}]:
                with self.subTest(payload=payload):
                    self.assertEqual(self.client.put("/knowledge-bases/3", json=payload).status_code, 422)
            connect.assert_not_called()

    def test_knowledge_base_mutations_return_404_for_missing_id(self):
        for method, kwargs in [(self.client.put, {"json": {"name": "name"}}),
                               (self.client.delete, {"params": {"confirm_name": "name"}})]:
            connection, cursor = self.connection()
            with patch.object(self.main, "get_conn", return_value=connection):
                self.assertEqual(method("/knowledge-bases/999", **kwargs).status_code, 404)
            self.assertEqual(cursor.execute.call_count, 1)

    def test_delete_requires_current_name_and_does_not_write_on_mismatch(self):
        connection, cursor = self.connection(row={"id": 3, "name": "renamed"})
        with patch.object(self.main, "get_conn", return_value=connection):
            self.assertEqual(self.client.delete("/knowledge-bases/3").status_code, 422)
            self.assertEqual(self.client.delete("/knowledge-bases/3", params={"confirm_name": "old name"}).status_code, 409)
        self.assertEqual(cursor.execute.call_count, 1)
        self.assertIn("FOR UPDATE", cursor.execute.call_args.args[0])

    def test_delete_purges_unshared_documents_and_processing_records(self):
        connection, cursor = self.connection(row={"id": 3, "name": "测试库"})
        cursor.fetchall.side_effect = [[{"id": 5}], [{"id": 5}], [{"id": 8}]]
        with patch.object(self.main, "get_conn", return_value=connection):
            response = self.client.delete("/knowledge-bases/3", params={"confirm_name": "测试库"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "deleted", "knowledge_base_id": 3,
            "name": "测试库", "deleted_document_count": 1, "retained_shared_document_count": 0})
        writes = [(call.args[0], call.args[1]) for call in cursor.execute.call_args_list[1:]
                  if len(call.args) > 1]
        self.assertIn(("DELETE FROM knowledge_bases WHERE id = %s", (3,)), writes)
        self.assertIn(("DELETE FROM documents WHERE id = ANY(%s::BIGINT[])", ([5],)), writes)
        self.assertIn(("DELETE FROM ingestion_batch_items WHERE document_id = ANY(%s::BIGINT[])", ([5],)), writes)
        self.assertIn(("DELETE FROM processing_leases WHERE resource = ANY(%s::TEXT[])",
                       (["litkg-document:5", "litkg-chunk:8"],)), writes)

    def test_extraction_replaces_model_page_guesses_with_parser_pages(self):
        from app import tasks
        from app.schemas import ClaimBundle

        chunk = {
            "id": 8, "document_id": 5, "section_title": "Results",
            "catalog_version": "test-version", "catalog_status": "done", "pipeline_generation": 1,
            "abbreviation_map": {},
            "text": "Aspirin inhibited COX-1.", "page_start": 2, "page_end": 2,
            "page_spans": [{"start": 0, "end": 24, "page_start": 2, "page_end": 2}],
        }
        for known_pages in [True, False]:
            with self.subTest(known_pages=known_pages):
                bundle = ClaimBundle.model_validate({"claims": [{
                    "subject": {"text": "Aspirin", "type": "Drug"},
                    "predicate": "INHIBITS", "object": {"text": "COX-1", "type": "Protein"},
                    "evidence": {"sentence": chunk["text"], "page": 999}, "confidence": 0.9,
                }]})
                if not known_pages:
                    chunk = {**chunk, "page_start": None, "page_end": None, "page_spans": []}
                connection, _ = self.connection(row=chunk)
                with patch.object(tasks, "get_conn", return_value=connection), patch.object(
                    tasks, "update_chunk_status"
                ), patch.object(tasks, "refresh_document_status"), patch.object(
                    tasks, "extract_claims", return_value=bundle
                ), patch.object(tasks, "load_catalog", return_value=[]), patch.object(
                    tasks, "extract_chunk_claims", return_value=bundle
                ), patch.object(tasks, "store_claim", return_value=(1, 1)) as store:
                    tasks.extract_chunk.run(8)
                evidence = store.call_args.kwargs["claim"].evidence
                self.assertEqual(evidence.page, 2 if known_pages else None)
                self.assertEqual(evidence.page_end, 2 if known_pages else None)
                self.assertEqual(evidence.section, "Results")


if __name__ == "__main__":
    unittest.main()
