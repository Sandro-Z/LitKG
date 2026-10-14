"""Optional PostgreSQL checks, using temporary schemas in a disposable test DB."""

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
from app.schemas import Claim


@unittest.skipUnless(os.environ.get("LITKG_TEST_DATABASE_URL"), "Set LITKG_TEST_DATABASE_URL to a disposable PostgreSQL database")
class GraphIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV

        with patch.dict(os.environ, TEST_ENV):
            from app import main
        cls.main = main
        cls.client = TestClient(main.app)
        cls.dsn = os.environ["LITKG_TEST_DATABASE_URL"]
        cls.schema = "litkg_test_" + uuid.uuid4().hex
        cls.migration = (Path(__file__).resolve().parents[1] / "db/init.sql").read_text()
        with psycopg.connect(cls.dsn) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        try:
            cls.seed()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def connect(cls):
        return psycopg.connect(cls.dsn, row_factory=dict_row, options=f"-csearch_path={cls.schema},public")

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    @classmethod
    def seed(cls):
        with cls.connect() as conn:
            conn.execute(cls.migration)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO knowledge_bases (slug, name) VALUES ('test-a', 'Scope A') RETURNING id")
                cls.kb_a = cur.fetchone()["id"]
                cur.execute("INSERT INTO knowledge_bases (slug, name) VALUES ('test-b', 'Scope B') RETURNING id")
                cls.kb_b = cur.fetchone()["id"]
                cls.documents = []
                for index, (kb, title, doi) in enumerate([
                    (cls.kb_a, "Alpha study", "10.1234/alpha"),
                    (cls.kb_b, "Private B study", "10.1234/beta"),
                    (cls.kb_a, "Gamma study", None),
                ]):
                    cur.execute(
                        "INSERT INTO documents (sha256, filename, source_path, title, doi, status) VALUES (%s, %s, %s, %s, %s, 'done') RETURNING id",
                        (f"test-{index}", f"paper-{index}.pdf", f"/test/paper-{index}.pdf", title, doi),
                    )
                    document_id = cur.fetchone()["id"]
                    cls.documents.append(document_id)
                    cur.execute("INSERT INTO knowledge_base_documents (knowledge_base_id, document_id) VALUES (%s, %s)", (kb, document_id))

                gefitinib = {"text": "Gefitinib", "type": "Drug", "normalized_id": "CHEBI:49668"}
                egfr = {"text": "EGFR", "type": "Protein", "normalized_id": "UniProt:P00533"}
                alias_egfr = {**egfr, "text": "Epidermal growth factor receptor"}
                mapk = {"text": "MAPK1", "type": "Gene"}
                next_index = 0

                def claim(document_id, subject, predicate, object_, sentence, **polarity):
                    nonlocal next_index
                    next_index += 1
                    cur.execute(
                        "INSERT INTO chunks (document_id, chunk_index, section_title, text, page_start, page_end, page_spans, status) VALUES (%s, %s, 'Results', %s, 2, 4, %s, 'done') RETURNING id",
                        (document_id, next_index, sentence, Jsonb([{"start": 0, "end": len(sentence), "page_start": 2, "page_end": 4}])),
                    )
                    chunk_id = cur.fetchone()["id"]
                    value = Claim.model_validate({
                        "subject": subject, "predicate": predicate, "object": object_,
                        "evidence": {"sentence": sentence, "page": 3, "page_end": 3},
                        "confidence": 0.9, **polarity,
                    })
                    return store_claim(cur, document_id=document_id, chunk_id=chunk_id, claim=value, extraction_model="test-only")

                a, b, c = cls.documents
                cls.claim_id, cls.relation_id = claim(a, gefitinib, "INHIBITS", egfr, "Gefitinib inhibits EGFR.")
                # Chunk overlap must not inflate the same document's evidence count.
                claim(a, gefitinib, "INHIBITS", egfr, "Gefitinib inhibits EGFR.")
                claim(b, gefitinib, "INHIBITS", egfr, "Evidence only in KB B.")
                claim(c, gefitinib, "INHIBITS", alias_egfr, "Gefitinib inhibits EGFR in cells.")
                claim(a, gefitinib, "INHIBITS", egfr, "Gefitinib did not inhibit EGFR.", negated=True)
                claim(a, gefitinib, "INHIBITS", egfr, "Gefitinib may inhibit EGFR.", speculative=True)
                claim(a, egfr, "ACTIVATES", mapk, "EGFR activates MAPK1.")
                claim(a, {"text": "Probe_100%", "type": "Compound"}, "BINDS_TO", egfr, "Probe_100% binds EGFR.")
                claim(a, {"text": "ProbeX100Y", "type": "Compound"}, "BINDS_TO", egfr, "ProbeX100Y binds EGFR.")
                claim(b, {"text": "Only_B", "type": "Drug"}, "BINDS_TO", mapk, "A relation present only in KB B.")
                cur.execute("SELECT id FROM entities WHERE normalized_id = 'UniProt:P00533'")
                cls.egfr_id = cur.fetchone()["id"]

    def get(self, resource, params=None, knowledge_base_id=None):
        kb = self.kb_a if knowledge_base_id is None else knowledge_base_id
        with patch.object(self.main, "get_conn", side_effect=self.connect):
            response = self.client.get(f"/knowledge-bases/{kb}/{resource}", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_name_alias_id_and_predicate_search(self):
        for query in ["egfr", "Epidermal growth factor receptor", "UniProt:P00533"]:
            with self.subTest(query=query):
                result = self.get("relations", {"query": query})
                self.assertEqual(result["total"], 6)
        self.assertEqual(self.get("relations", {"query": "BINDS_TO"})["total"], 2)
        entities = self.get("entities", {"query": "Epidermal growth factor receptor"})["entities"]
        self.assertEqual([entity["id"] for entity in entities], [self.egfr_id])
        self.assertEqual(entities[0]["document_count"], 2)

    def test_scope_counts_deduplication_and_evidence_locations(self):
        result = self.get("relations", {"predicate": "inhibits", "include_negated": False, "include_speculative": False, "evidence_limit": 20})
        self.assertEqual(result["total"], 1)
        relation = result["relations"][0]
        self.assertEqual(relation["document_count"], 2)
        self.assertEqual(relation["evidence_count"], 2)
        samples = relation["evidence_samples"]
        self.assertEqual({sample["document_id"] for sample in samples}, {self.documents[0], self.documents[2]})
        alpha = next(sample for sample in samples if sample["document_id"] == self.documents[0])
        self.assertEqual(alpha["title"], "Alpha study")
        self.assertEqual(alpha["doi"], "10.1234/alpha")
        self.assertEqual((alpha["page"], alpha["page_end"]), (3, 3))
        self.assertEqual(self.get("relations", {"query": "Only_B"})["total"], 0)
        self.assertEqual(self.get("entities", {"query": "Only_B"})["entities"], [])
        b = self.get("relations", {"predicate": "INHIBITS"}, knowledge_base_id=self.kb_b)["relations"][0]
        self.assertEqual(b["document_count"], 1)
        self.assertEqual(b["evidence_samples"][0]["title"], "Private B study")

    def test_direction_subject_object_and_document_threshold(self):
        incoming = self.get("relations", {"entity_id": self.egfr_id, "direction": "incoming"})
        outgoing = self.get("relations", {"entity_id": self.egfr_id, "direction": "outgoing"})
        self.assertEqual((incoming["total"], outgoing["total"]), (5, 1))
        self.assertEqual(outgoing["relations"][0]["predicate"], "ACTIVATES")
        result = self.get("relations", {
            "subject": "CHEBI:49668", "object": "Epidermal growth factor receptor",
            "object_entity_id": self.egfr_id, "min_document_count": 2,
        })
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["relations"][0]["id"], self.relation_id)
        self.assertEqual(self.get("relations", {"min_document_count": 3})["total"], 0)

    def test_literal_wildcards_and_pagination(self):
        result = self.get("relations", {"query": "Probe_100%"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["relations"][0]["subject"]["text"], "Probe_100%")
        entities = self.get("entities", {"query": "Probe_100%"})["entities"]
        self.assertEqual([entity["canonical_name"] for entity in entities], ["Probe_100%"])
        self.assertEqual(self.get("relations", {"query": "' OR 1=1 --"})["total"], 0)
        first = self.get("relations", {"limit": 1, "offset": 0})
        second = self.get("relations", {"limit": 1, "offset": 1})
        self.assertEqual((first["total"], second["total"]), (6, 6))
        self.assertNotEqual(first["relations"][0]["id"], second["relations"][0]["id"])
        past = self.get("relations", {"limit": 1, "offset": 999})
        self.assertEqual(past["total"], 6)
        self.assertEqual(past["relations"], [])

    def test_claims_and_subgraph_return_consistent_provenance(self):
        claims = self.get("claims")["claims"]
        claim = next(value for value in claims if value["id"] == self.claim_id)
        self.assertEqual(claim["doi"], "10.1234/alpha")
        self.assertEqual(claim["page"], 3)
        with patch.object(self.main, "get_conn", side_effect=self.connect):
            response = self.client.get(f"/documents/{self.documents[0]}/claims")
        self.assertEqual(response.status_code, 200)
        single = next(value for value in response.json()["claims"] if value["id"] == self.claim_id)
        self.assertEqual(single, claim)
        graph = self.get("graph", {"entity_id": self.egfr_id, "direction": "outgoing"})
        self.assertEqual(graph["summary"]["relation_count"], 1)
        self.assertEqual(graph["edges"][0]["predicate"], "ACTIVATES")
        self.assertEqual(graph["edges"][0]["evidence_samples"][0]["doi"], "10.1234/alpha")

    def test_parse_and_extraction_tasks_persist_provenance(self):
        from app import tasks
        from app.schemas import ClaimBundle

        sentence = "Gefitinib inhibits EGFR."
        chunk = {
            "section_title": "Results", "text": sentence, "token_estimate": 10,
            "page_start": 4, "page_end": 4,
            "page_spans": [{"start": 0, "end": len(sentence), "page_start": 4, "page_end": 4}],
        }
        with self.connect() as conn:
            document_id = conn.execute(
                "INSERT INTO documents (sha256, filename, source_path) VALUES ('parser-task', 'parser.pdf', '/test/parser.pdf') RETURNING id"
            ).fetchone()["id"]
        try:
            with patch.object(tasks, "get_conn", side_effect=self.connect), patch.object(
                tasks, "parse_pdf_to_chunks", return_value=({"title": "Parser study", "doi": "10.1234/parser"}, [chunk])
            ), patch.object(tasks, "update_document_status"), patch.object(tasks.extract_chunk, "apply_async") as enqueue:
                result = tasks.parse_document.run(document_id)
            self.assertEqual(result["chunk_count"], 1)
            chunk_id = enqueue.call_args.kwargs["args"][0]
            with self.connect() as conn:
                document = conn.execute("SELECT title, doi FROM documents WHERE id = %s", (document_id,)).fetchone()
                stored_chunk = conn.execute("SELECT page_start, page_end, page_spans FROM chunks WHERE id = %s", (chunk_id,)).fetchone()
            self.assertEqual(document, {"title": "Parser study", "doi": "10.1234/parser"})
            self.assertEqual(stored_chunk["page_spans"], chunk["page_spans"])
            self.assertEqual((stored_chunk["page_start"], stored_chunk["page_end"]), (4, 4))
            bundle = ClaimBundle.model_validate({"claims": [{
                "subject": {"text": "Gefitinib", "type": "Drug", "normalized_id": "CHEBI:49668"},
                "predicate": "INHIBITS",
                "object": {"text": "EGFR", "type": "Protein", "normalized_id": "UniProt:P00533"},
                "evidence": {"sentence": sentence, "page": 999}, "confidence": 0.9,
            }]})
            with patch.object(tasks, "get_conn", side_effect=self.connect), patch.object(
                tasks, "extract_claims", return_value=bundle
            ), patch.object(tasks, "refresh_document_status"):
                tasks.extract_chunk.run(chunk_id)
            with self.connect() as conn:
                stored = conn.execute("SELECT claim_json FROM claims WHERE chunk_id = %s", (chunk_id,)).fetchone()
            self.assertEqual(stored["claim_json"]["evidence"]["page"], 4)
            self.assertEqual(stored["claim_json"]["evidence"]["page_end"], 4)
        finally:
            with self.connect() as conn:
                conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))

    def test_migration_handles_old_columns_and_is_idempotent(self):
        schema = "litkg_legacy_test_" + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row, options=f"-csearch_path={schema},public") as conn:
                conn.execute("""
                    CREATE TABLE documents (
                        id BIGSERIAL PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL,
                        filename TEXT NOT NULL, source_path TEXT NOT NULL, title TEXT,
                        status TEXT NOT NULL DEFAULT 'uploaded', error TEXT,
                        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
                    );
                    CREATE TABLE chunks (
                        id BIGSERIAL PRIMARY KEY, document_id BIGINT REFERENCES documents(id),
                        chunk_index INT NOT NULL, section_title TEXT, text TEXT NOT NULL,
                        token_estimate INT, status TEXT DEFAULT 'queued', error TEXT,
                        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
                        UNIQUE (document_id, chunk_index)
                    );
                    INSERT INTO documents (sha256, filename, source_path) VALUES ('legacy', 'legacy.pdf', '/test/legacy.pdf');
                    INSERT INTO chunks (document_id, chunk_index, section_title, text) VALUES (1, 0, 'Page 7', 'Legacy evidence.');
                """)
                conn.execute(self.migration)
                conn.execute(self.migration)
                row = conn.execute("SELECT page_start, page_end, page_spans FROM chunks WHERE id = 1").fetchone()
                self.assertEqual((row["page_start"], row["page_end"]), (7, 7))
                self.assertEqual(row["page_spans"][0]["page_start"], 7)
                self.assertIsNone(conn.execute("SELECT doi FROM documents WHERE id = 1").fetchone()["doi"])
        finally:
            with psycopg.connect(self.dsn) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
