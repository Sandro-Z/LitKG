import os
from pathlib import Path
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient

from app.graph_store import store_claim, upsert_entity
from app.normalization import entity_family, entity_identity, normalize_alias, qualifiers_fingerprint
from app.schemas import Claim


@unittest.skipUnless(os.environ.get("LITKG_TEST_DATABASE_URL"), "Requires a disposable PostgreSQL database")
class EntityResolutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import main, tasks
        self.main, self.tasks = main, tasks
        self.client = TestClient(main.app)
        self.dsn = os.environ["LITKG_TEST_DATABASE_URL"]
        self.schema = "litkg_resolution_test_" + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        with self.connect() as conn:
            conn.execute((Path(__file__).resolve().parents[1] / "db/init.sql").read_text())
            self.kb = conn.execute("INSERT INTO knowledge_bases (slug, name) VALUES ('a', 'A') RETURNING id").fetchone()["id"]
            self.other_kb = conn.execute("INSERT INTO knowledge_bases (slug, name) VALUES ('b', 'B') RETURNING id").fetchone()["id"]

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, options=f"-csearch_path={self.schema},public")

    def drop_schema(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def document(self, text, kb=None, status="done"):
        with self.connect() as conn:
            document_id = conn.execute(
                "INSERT INTO documents (sha256, filename, source_path, title, doi, status) VALUES (%s, 'test.pdf', '/test/test.pdf', 'Test study', '10.1234/test', %s) RETURNING id",
                (uuid.uuid4().hex, status),
            ).fetchone()["id"]
            conn.execute("INSERT INTO knowledge_base_documents (knowledge_base_id, document_id) VALUES (%s, %s)", (self.kb if kb is None else kb, document_id))
            chunk_id = conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, section_title, text, page_start, page_end, status) VALUES (%s, 0, 'Results', %s, 1, 1, 'done') RETURNING id",
                (document_id, text),
            ).fetchone()["id"]
        return document_id, chunk_id

    def claim(self, subject, object_, sentence="Test evidence.", **kwargs):
        return Claim.model_validate({
            "subject": subject, "predicate": "INHIBITS", "object": object_,
            "evidence": {"sentence": sentence, "page": 1}, "confidence": 0.9, **kwargs,
        })

    def legacy_claim(self, document, claim):
        document_id, chunk_id = document
        with self.connect() as conn:
            def old_entity(entity):
                key = f"{entity.type.lower()}|" + (f"id:{entity.normalized_id.casefold()}" if entity.normalized_id else f"name:{normalize_alias(entity.text)}")
                row = conn.execute(
                    "INSERT INTO entities (canonical_key, canonical_name, entity_type, normalized_id) VALUES (%s, %s, %s, %s) ON CONFLICT (canonical_key) DO UPDATE SET updated_at = now() RETURNING id",
                    (key, entity.text, entity.type, entity.normalized_id),
                ).fetchone()
                conn.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (row["id"], entity.text, normalize_alias(entity.text)))
                if entity.normalized_id:
                    conn.execute("INSERT INTO entity_identifiers (entity_id, entity_family, normalized_id, identifier_key) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (row["id"], entity_family(entity.type), entity.normalized_id, entity.normalized_id.casefold()))
                return row["id"]
            subject, object_ = old_entity(claim.subject), old_entity(claim.object)
            claim_id = conn.execute(
                """
                INSERT INTO claims (document_id, chunk_id, subject_text, subject_type, subject_entity_id, predicate,
                    object_text, object_type, object_entity_id, evidence_text, confidence, negated, speculative, claim_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (document_id, chunk_id, claim.subject.text, claim.subject.type, subject, claim.predicate,
                 claim.object.text, claim.object.type, object_, claim.evidence.sentence, claim.confidence,
                 claim.negated, claim.speculative, Jsonb(claim.model_dump(mode="json"))),
            ).fetchone()["id"]
            relation_id = conn.execute(
                """
                INSERT INTO relations (subject_entity_id, predicate, object_entity_id, qualifiers, qualifiers_hash, negated, speculative)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (subject_entity_id, predicate, object_entity_id, qualifiers_hash, negated, speculative)
                DO UPDATE SET updated_at = now() RETURNING id
                """,
                (subject, claim.predicate, object_, Jsonb(claim.qualifiers), qualifiers_fingerprint(claim.qualifiers), claim.negated, claim.speculative),
            ).fetchone()["id"]
            conn.execute("INSERT INTO relation_evidence (relation_id, claim_id) VALUES (%s, %s)", (relation_id, claim_id))
        return claim_id

    def get(self, resource, params=None, kb=None):
        with patch.object(self.main, "get_conn", side_effect=self.connect):
            return self.client.get(f"/knowledge-bases/{self.kb if kb is None else kb}/{resource}", params=params)

    def rebuild(self, job_id=None):
        with patch.object(self.tasks, "get_conn", side_effect=self.connect), patch.object(
            self.tasks, "extract_claims", side_effect=AssertionError("Rebuild must not call the LLM")
        ):
            return self.tasks.rebuild_knowledge_graph.run(self.kb, job_id)

    def test_legacy_fragments_connect_after_document_abbreviation_resolution(self):
        first = self.document("Interleukin 6 (IL-6). DrugA inhibits IL-6.")
        second = self.document("Interleukin 6 (IL6). IL6 activates STAT3.")
        claims = [
            self.claim({"text": "DrugA", "type": "Drug"}, {"text": "IL-6", "type": "Protein"}, "DrugA inhibits IL-6."),
            self.claim({"text": "IL6", "type": "Protein"}, {"text": "STAT3", "type": "Protein"}, "IL6 activates STAT3.", predicate="ACTIVATES"),
        ]
        claim_ids = [self.legacy_claim(document, claim) for document, claim in zip([first, second], claims)]
        report = self.rebuild()
        self.assertEqual(report["before"]["component_count"], 2)
        self.assertEqual(report["before"]["largest_component_ratio"], 0.5)
        self.assertEqual(report["after"]["component_count"], 1)
        self.assertEqual(report["after"]["node_count"], 3)
        self.assertEqual(report["after"]["largest_component_ratio"], 1.0)
        stored = self.get("claims").json()["claims"]
        self.assertEqual([row["id"] for row in stored], claim_ids)
        for row, original in zip(stored,claims):
            self.assertEqual(row['claim_json']['semantic_status'],'pending')
            for field in ('subject','object','evidence'):
                self.assertEqual(row['claim_json'][field],original.model_dump(mode='json')[field])
            with self.connect() as conn:
                snapshots=conn.execute('SELECT snapshot FROM claim_history WHERE original_claim_id=%s',(row['id'],)).fetchall()
                self.assertIn(original.model_dump(mode='json'),[s['snapshot']['claim_json'] for s in snapshots])
        self.assertEqual(stored[0]["object_entity_id"], stored[1]["subject_entity_id"])
        self.assertEqual(stored[0]["doi"], "10.1234/test")
        repeated = self.rebuild()
        self.assertEqual(repeated["after"], report["after"])
        self.assertEqual(len(self.get("claims").json()["claims"]), 2)

    def test_legacy_duplicate_chemical_ids_merge_and_preserve_all_evidence(self):
        first, second = self.document("Aspirin evidence one."), self.document("Aspirin evidence two.")
        object_ = {"text": "COX-1", "type": "Protein"}
        first_id = self.legacy_claim(first, self.claim({"text": "Aspirin", "type": "Drug", "normalized_id": "CHEBI:15365"}, object_, "Aspirin evidence one."))
        second_id = self.legacy_claim(second, self.claim({"text": "Aspirin", "type": "Compound", "normalized_id": "PubChem:2244"}, object_, "Aspirin evidence two."))
        report = self.rebuild()
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual((report["before"]["node_count"], report["after"]["node_count"]), (3, 2))
        # Synthetic legacy quotes do not establish a measured inhibition. Keep
        # them reviewable while checking identity/evidence preservation.
        self.assertEqual(self.get('relations').json()['relations'],[])
        relation = self.get("relations",params={'include_pending':True}).json()["relations"][0]
        self.assertEqual((relation["document_count"], relation["evidence_count"]), (2, 2))
        self.assertEqual({row["id"] for row in self.get("claims").json()["claims"]}, {first_id, second_id})
        entities = self.get("entities", {"query": "PubChem:2244", "include_pending":True}).json()["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(set(entities[0]["identifiers"]), {"CHEBI:15365", "PubChem:2244"})

    def test_ambiguous_short_symbols_and_generic_labels_are_not_joined(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                first = upsert_entity(cur, entity_identity("AR", "Protein", abbreviations={"ar": "androgen receptor"}, document_id=1))
                second = upsert_entity(cur, entity_identity("AR", "Protein", abbreviations={"ar": "aldose reductase"}, document_id=2))
                ambiguous = upsert_entity(cur, entity_identity("AR", "Protein", abbreviations={"ar": None}, document_id=3))
                compound_a = upsert_entity(cur, entity_identity("compound 1", "Drug", "CHEBI:15365", document_id=1))
                compound_b = upsert_entity(cur, entity_identity("compound 1", "Compound", "PubChem:12345", document_id=2))
                unknown_a = upsert_entity(cur, entity_identity("Unknown", "Drug", "CHEBI:1"))
                unknown_b = upsert_entity(cur, entity_identity("Unknown", "Compound", "PubChem:2"))
        self.assertEqual(len({first, second, ambiguous}), 3)
        self.assertNotEqual(compound_a, compound_b)
        self.assertNotEqual(unknown_a, unknown_b)

    def test_conflicting_ids_and_gene_protein_types_remain_separate(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                first = upsert_entity(cur, entity_identity("Aspirin", "Drug", "PubChem:2244"))
                conflicting = upsert_entity(cur, entity_identity("Aspirin", "Compound", "PubChem:2245"))
                gene = upsert_entity(cur, entity_identity("MET", "Gene"))
                protein = upsert_entity(cur, entity_identity("MET", "Protein"))
        self.assertNotEqual(first, conflicting)
        self.assertNotEqual(gene, protein)

    def test_concurrent_identifier_formats_and_labels_use_one_node(self):
        inputs = [("Drug", "CHEBI:15365"), ("Compound", "https://purl.obolibrary.org/obo/CHEBI_15365"), ("Drug", "chebi: 015365"), ("Compound", "PubChem CID:2244")]
        def register(value):
            entity_type, identifier = value
            with self.connect() as conn:
                with conn.cursor() as cur:
                    return upsert_entity(cur, entity_identity("Aspirin", entity_type, identifier))
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(register, inputs))
        self.assertEqual(len(set(ids)), 1)

    def test_diagnostics_include_edges_outside_display_limit(self):
        for index, (subject, object_) in enumerate([("A", "B"), ("C", "D"), ("E", "E")]):
            document_id, chunk_id = self.document(f"Evidence {index}.")
            with self.connect() as conn:
                with conn.cursor() as cur:
                    store_claim(cur, document_id=document_id, chunk_id=chunk_id, extraction_model="test", claim=self.claim({"text": subject, "type": "Protein"}, {"text": object_, "type": "Protein"}, f"Evidence {index}."))
        graph = self.get("graph", {"limit": 1}).json()
        metrics = self.get("graph/diagnostics").json()
        self.assertEqual(graph["summary"]["relation_count"], 1)
        self.assertEqual((metrics["node_count"], metrics["component_count"]), (5, 3))
        self.assertEqual(metrics["isolated_node_count"], 1)
        self.assertEqual(metrics["self_loop_relation_count"], 1)
        self.assertEqual(self.get("graph/diagnostics", kb=self.other_kb).json()["node_count"], 0)

    def test_rebuild_job_status_is_persisted_and_scoped(self):
        with patch.object(self.main, "get_conn", side_effect=self.connect), patch.object(self.tasks.rebuild_knowledge_graph, "apply_async"):
            response = self.client.post(f"/knowledge-bases/{self.kb}/rebuild-graph")
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["task_id"]
        self.rebuild(job_id)
        status = self.get(f"rebuild-graph/{job_id}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "done")
        self.assertIn("before", status.json()["result"])
        self.assertEqual(self.get(f"rebuild-graph/{job_id}", kb=self.other_kb).status_code, 404)

    def test_active_documents_are_skipped(self):
        self.document("Interleukin 6 (IL-6).", status="extracting")
        result = self.rebuild()
        self.assertEqual(result["skipped_active_document_count"], 1)
        self.assertEqual(result["projected_count"], 0)

    def test_active_chunk_is_skipped_even_if_document_status_is_stale(self):
        _, chunk_id = self.document("Interleukin 6 (IL-6).")
        with self.connect() as conn:
            conn.execute("UPDATE chunks SET status = 'extracting' WHERE id = %s", (chunk_id,))
        self.assertEqual(self.rebuild()["skipped_active_document_count"], 1)

    def test_global_merge_preserves_other_knowledge_base_evidence_and_polarity(self):
        first = self.document("Evidence in A.")
        second = self.document("Evidence in B.", kb=self.other_kb)
        object_ = {"text": "COX-1", "type": "Protein"}
        self.legacy_claim(first, self.claim({"text": "Aspirin", "type": "Drug", "normalized_id": "CHEBI:15365"}, object_, "Evidence in A."))
        original = self.claim({"text": "Aspirin", "type": "Compound", "normalized_id": "PubChem:2244"}, object_, "Evidence in B.", negated=True, speculative=True, qualifiers={"assay": "binding"})
        other_claim_id = self.legacy_claim(second, original)
        self.assertEqual(self.rebuild()["failed_count"], 0)
        relation = self.get("relations", kb=self.other_kb).json()["relations"][0]
        self.assertTrue(relation["negated"])
        self.assertTrue(relation["speculative"])
        self.assertEqual(relation["qualifiers"], {"assay": "binding"})
        self.assertEqual(relation["document_count"], 1)
        self.assertEqual(relation["evidence_samples"][0]["document_id"], second[0])
        other_claim = self.get("claims", kb=self.other_kb).json()["claims"][0]
        self.assertEqual(other_claim["id"], other_claim_id)
        self.assertEqual(other_claim["claim_json"], original.model_dump(mode="json"))
        first_relation = self.get("relations",params={'include_pending':True}).json()["relations"][0]
        self.assertEqual(first_relation["evidence_samples"][0]["document_id"], first[0])

    def test_enqueue_failure_is_recorded_without_leaving_a_queued_job(self):
        with patch.object(self.main, "get_conn", side_effect=self.connect), patch.object(
            self.tasks.rebuild_knowledge_graph, "apply_async", side_effect=RuntimeError("broker unavailable")
        ):
            response = self.client.post(f"/knowledge-bases/{self.kb}/rebuild-graph")
        self.assertEqual(response.status_code, 503)
        with self.connect() as conn:
            job = conn.execute("SELECT status, error FROM graph_rebuild_jobs WHERE knowledge_base_id = %s", (self.kb,)).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "Could not enqueue graph rebuild")


if __name__ == "__main__":
    unittest.main()
