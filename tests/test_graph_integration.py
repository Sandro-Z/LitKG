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
from pdf_fixtures import text_pdf


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

    def test_compact_and_explicit_comparison_keep_both_claims_on_one_relation(self):
        from app.graph_query import fetch_graph
        from app.schemas import Process
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO documents (sha256,filename,source_path,status) VALUES (%s,'comparison.pdf','/test/comparison.pdf','done') RETURNING id",
                            ('comparison-' + uuid.uuid4().hex,))
                document_id = cur.fetchone()['id']
                sentence = 'AKT1 abundance was higher in responders than nonresponders.'
                claim = Claim.model_validate({
                    'subject': {'text': 'AKT1', 'type': 'Protein'},
                    'predicate': 'DIFFERS_BETWEEN',
                    'object': {'text': 'responders', 'type': 'StudyGroup'},
                    'qualifiers': {'metric': 'abundance', 'direction': 'increased',
                                   'comparison_group': 'responders', 'reference_group': 'nonresponders'},
                    'evidence': {'sentence': sentence}, 'confidence': .9})
                claims, relations = [], []
                for index in range(2):
                    cur.execute("INSERT INTO chunks (document_id,chunk_index,text) VALUES (%s,%s,%s) RETURNING id",
                                (document_id, index, sentence))
                    chunk_id = cur.fetchone()['id']
                    if index:
                        claim.subject.process = Process(kind='abundance', direction='increased')
                    claim_id, relation_id = store_claim(cur, document_id=document_id, chunk_id=chunk_id,
                                                        claim=claim, extraction_model='regression')
                    claims.append(claim_id)
                    relations.append(relation_id)
                self.assertNotEqual(claims[0], claims[1])
                self.assertEqual(relations[0], relations[1])
                graph = fetch_graph(cur, document_id=document_id)
                self.assertEqual(len(graph['edges']), 1)
                self.assertEqual(graph['edges'][0]['evidence_count'], 1)
                cur.execute('SELECT claim_json FROM claims WHERE id=ANY(%s) ORDER BY id', (claims,))
                raw = [row['claim_json'] for row in cur.fetchall()]
                self.assertIsNone(raw[0]['subject']['process'])
                self.assertEqual(raw[1]['subject']['process']['kind'], 'abundance')
                cur.execute('SELECT count(*) AS count,count(DISTINCT fact_key) AS keys FROM facts WHERE claim_id=ANY(%s)', (claims,))
                self.assertEqual(cur.fetchone(), {'count': 2, 'keys': 1})
            conn.rollback()

    def test_legacy_comparison_repair_preserves_same_chunk_claims_and_is_idempotent(self):
        import importlib.util
        import tempfile
        from app.graph_query import fetch_graph
        from app.schemas import Process
        spec = importlib.util.spec_from_file_location('comparison_repair',
            Path(__file__).resolve().parents[1] / 'scripts/repair_comparison_relations.py')
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.connect() as conn, tempfile.TemporaryDirectory() as backup_dir:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO documents(sha256,filename,source_path,status) VALUES (%s,'legacy.pdf','/test/legacy.pdf','done') RETURNING id",
                            ('legacy-' + uuid.uuid4().hex,))
                document_id = cur.fetchone()['id']
                cur.execute('INSERT INTO ingestion_batches(knowledge_base_id) VALUES (%s) RETURNING id', (self.kb_a,))
                batch_id = cur.fetchone()['id']
                cur.execute("INSERT INTO ingestion_batch_items(batch_id,position,filename,document_id) VALUES (%s,0,'legacy.pdf',%s)",
                            (batch_id, document_id))
                sentence = 'LegacyProtein abundance was higher in responders than nonresponders.'
                cur.execute('INSERT INTO chunks(document_id,chunk_index,text) VALUES (%s,0,%s) RETURNING id', (document_id, sentence))
                chunk_id = cur.fetchone()['id']
                claim = Claim.model_validate({
                    'subject': {'text': 'LegacyProtein', 'type': 'Protein'}, 'predicate': 'DIFFERS_BETWEEN',
                    'object': {'text': 'responders', 'type': 'StudyGroup'},
                    'qualifiers': {'metric': 'abundance', 'direction': 'increased',
                                   'comparison_group': 'responders', 'reference_group': 'nonresponders'},
                    'evidence': {'sentence': sentence}, 'confidence': .9})
                with patch('app.facts.canonical_comparison_qualifiers', side_effect=lambda predicate, qualifiers: qualifiers):
                    compact_id, compact_relation = store_claim(cur, document_id=document_id, chunk_id=chunk_id,
                                                               claim=claim, extraction_model='legacy')
                    claim.subject.process = Process(kind='abundance', direction='increased')
                    explicit_id, explicit_relation = store_claim(cur, document_id=document_id, chunk_id=chunk_id,
                                                                 claim=claim, extraction_model='legacy')
                self.assertNotEqual(compact_relation, explicit_relation)
                before = cur.execute('SELECT id,claim_json FROM claims WHERE document_id=%s ORDER BY id', (document_id,)).fetchall()
                preview = migration.repair(conn, batch_id)
                self.assertEqual(preview['merged_relations'], 1)
                self.assertEqual(len(fetch_graph(cur, document_id=document_id)['edges']), 2)
                result = migration.repair(conn, batch_id, backup_dir, apply=True)
                self.assertEqual(result['retained_legacy_fingerprints'], [{'claim_id': compact_id, 'canonical_claim_id': explicit_id}])
                self.assertTrue(Path(result['backup']).is_file())
                self.assertEqual(len(fetch_graph(cur, document_id=document_id)['edges']), 1)
                after = cur.execute('SELECT id,claim_json FROM claims WHERE document_id=%s ORDER BY id', (document_id,)).fetchall()
                self.assertEqual(before, after)
                self.assertEqual(cur.execute('SELECT count(*) AS n FROM fact_evidence ev JOIN facts f ON f.id=ev.fact_id WHERE f.claim_id=ANY(%s)',
                                             ([compact_id, explicit_id],)).fetchone()['n'], 2)
                self.assertEqual(migration.repair(conn, batch_id)['normalized_relations'], 0)
            conn.rollback()

    def test_proteomics_forms_with_parent_accession_remain_separate_in_database(self):
        from app.graph_store import upsert_entity
        from app.normalization import entity_identity
        with self.connect() as conn:
            cur = conn.cursor()
            names = ['AKT1 form A', 'AKT1 form B', 'AKT1 S473 phosphorylated', 'AKT1 T308 phosphorylated']
            ids = [upsert_entity(cur, entity_identity(name, 'Proteoform', 'UniProt:P31749')) for name in names]
            self.assertEqual(len(set(ids)), len(names))
            repeated = upsert_entity(cur, entity_identity(names[0], 'Proteoform', 'UniProt:P31749'))
            self.assertEqual(repeated, ids[0])
            parent = upsert_entity(cur, entity_identity('AKT1', 'Protein', 'UniProt:P31749'))
            self.assertNotIn(parent, ids)
            rows = cur.execute('SELECT entity_id,identifier_key FROM entity_identifiers WHERE entity_id=ANY(%s)', (ids,)).fetchall()
            self.assertEqual(len({row['identifier_key'] for row in rows}), len(names))
            conn.rollback()

    def test_proteomics_group_and_sample_labels_do_not_merge_between_documents(self):
        from app.graph_store import upsert_entity
        from app.normalization import entity_identity
        with self.connect() as conn:
            for kind, label in [('ProteinGroup', 'group 1'), ('Biospecimen', 'plasma'), ('Peptide', 'peptide 1')]:
                identifier = 'UniProt:P31749' if kind in {'ProteinGroup', 'Peptide'} else None
                ids = [upsert_entity(conn.cursor(), entity_identity(label, kind, identifier, document_id=doc))
                       for doc in self.documents[:2]]
                self.assertNotEqual(ids[0], ids[1], kind)
            conn.rollback()

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
            ), patch.object(tasks, "update_document_status"), patch.object(tasks.build_document_catalog, "apply_async") as enqueue, patch.object(tasks.extract_chunk, 'apply_async') as relationships:
                result = tasks.parse_document.run(document_id)
            self.assertEqual(result["chunk_count"], 1)
            enqueue.assert_called_once_with(args=[document_id], queue='extract')
            relationships.assert_not_called()
            from app.schemas import CatalogBundle
            catalog = CatalogBundle.model_validate({'entities': [
                {'text':'Gefitinib','type':'Drug','evidence':sentence},
                {'text':'EGFR','type':'Protein','evidence':sentence},
            ]})
            with patch.object(tasks,'get_conn',side_effect=self.connect), patch.object(tasks,'extract_catalog',return_value=catalog), patch.object(tasks.extract_chunk,'apply_async') as relationships:
                tasks.build_document_catalog.run(document_id)
            chunk_id = relationships.call_args.kwargs['args'][0]
            with self.connect() as conn:
                document = conn.execute("SELECT title, doi FROM documents WHERE id = %s", (document_id,)).fetchone()
                stored_chunk = conn.execute("SELECT page_start, page_end, page_spans FROM chunks WHERE id = %s", (chunk_id,)).fetchone()
            self.assertEqual(document, {"title": "Parser study", "doi": "10.1234/parser"})
            self.assertEqual(stored_chunk["page_spans"], chunk["page_spans"])
            self.assertEqual((stored_chunk["page_start"], stored_chunk["page_end"]), (4, 4))
            bundle = ClaimBundle.model_validate({"claims": [{
                "subject": {"text": "Gefitinib", "type": "Drug"},
                "predicate": "INHIBITS",
                "object": {"text": "EGFR", "type": "Protein"},
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

    def test_page_repair_preserves_claims_graph_and_supports_dry_run_and_retry(self):
        from app import page_repair, tasks

        first, second = "Aspirin inhibited COX-1.", "EGFR binds gefitinib."
        with text_pdf(["", first, second]) as pdf:
            with self.connect() as conn:
                document_id = conn.execute(
                    "INSERT INTO documents (sha256, filename, source_path, doi, status) "
                    "VALUES ('page-repair', 'repair.pdf', %s, '10.1234/repair', 'done') RETURNING id",
                    (str(pdf),),
                ).fetchone()["id"]
                conn.execute("INSERT INTO knowledge_base_documents (knowledge_base_id, document_id) VALUES (%s, %s)",
                             (self.kb_a, document_id))
                chunk_id = conn.execute(
                    "INSERT INTO chunks (document_id, chunk_index, text, status) VALUES (%s, 0, %s, 'done') RETURNING id",
                    (document_id, first + " " + second),
                ).fetchone()["id"]
                for sentence in (first, second):
                    claim = Claim.model_validate({
                        "subject": {"text": "Page repair drug", "type": "Drug"}, "predicate": "INHIBITS",
                        "object": {"text": "Page repair target", "type": "Protein"},
                        "evidence": {"sentence": sentence, "page": 999, "section": "Results"},
                        "qualifiers": {"condition": "original"}, "confidence": 0.9, "negated": True,
                    })
                    store_claim(conn.cursor(), document_id=document_id, chunk_id=chunk_id,
                                claim=claim, extraction_model="test-only")
            try:
                def snapshot():
                    with self.connect() as conn:
                        return {
                            "document": conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone(),
                            "chunk": conn.execute("SELECT * FROM chunks WHERE id = %s", (chunk_id,)).fetchone(),
                            "claims": conn.execute("SELECT * FROM claims WHERE document_id = %s ORDER BY id", (document_id,)).fetchall(),
                            "edges": conn.execute("SELECT * FROM relation_evidence WHERE claim_id IN "
                                                  "(SELECT id FROM claims WHERE document_id = %s) ORDER BY claim_id", (document_id,)).fetchall(),
                        }

                original = snapshot()
                with patch.object(page_repair, "get_conn", side_effect=self.connect), patch.object(
                    tasks, "extract_claims", side_effect=AssertionError("Page repair must not call the LLM"),
                ):
                    preview = page_repair.repair_pages(document_id=document_id, dry_run=True)["documents"][0]
                    self.assertEqual((preview["changed_chunk_count"], preview["changed_claim_count"]), (1, 2))
                    self.assertEqual(snapshot(), original)
                    # A busy claim must roll back any chunk updates made earlier.
                    with self.connect() as locked:
                        locked.execute("SELECT id FROM claims WHERE document_id = %s FOR UPDATE", (document_id,))
                        report = page_repair.repair_document_pages(document_id)
                        self.assertEqual(report["status"], "skipped_active")
                    self.assertEqual(snapshot(), original)
                    report = page_repair.repair_pages(document_id=document_id)["documents"][0]
                    self.assertEqual(report["located_claim_count"], 2)
                    self.assertEqual(report["unresolved_claim_count"], 0)
                    second_run = page_repair.repair_pages(document_id=document_id)["documents"][0]
                    self.assertEqual((second_run["changed_chunk_count"], second_run["changed_claim_count"]), (0, 0))
                repaired = snapshot()
                self.assertEqual(repaired["document"], original["document"])
                self.assertEqual(repaired["chunk"]["text"], original["chunk"]["text"])
                self.assertEqual(repaired["edges"], original["edges"])
                for before, after, expected_page in zip(original["claims"], repaired["claims"], (2, 3)):
                    self.assertEqual({k: v for k, v in before.items() if k != "claim_json"},
                                     {k: v for k, v in after.items() if k != "claim_json"})
                    expected_json = {**before["claim_json"], "evidence": {
                        **before["claim_json"]["evidence"], "page": expected_page, "page_end": expected_page,
                    }}
                    self.assertEqual(after["claim_json"], expected_json)
                with patch.object(self.main, "get_conn", side_effect=self.connect):
                    claims = self.client.get(f"/documents/{document_id}/claims").json()["claims"]
                self.assertEqual([claim["page"] for claim in claims], [2, 3])
                relation = self.get("relations", {"subject": "Page repair drug"})["relations"][0]
                self.assertEqual({sample["page"] for sample in relation["evidence_samples"]}, {2, 3})

                for document_status, chunk_status in [("parsing", "done"), ("done", "extracting"), ("done", "queued")]:
                    with self.connect() as conn:
                        conn.execute("UPDATE documents SET status = %s WHERE id = %s", (document_status, document_id))
                        conn.execute("UPDATE chunks SET status = %s WHERE id = %s", (chunk_status, chunk_id))
                    with patch.object(page_repair, "get_conn", side_effect=self.connect), patch.object(
                        page_repair, "fallback_pdf_text", side_effect=AssertionError("Skip active documents before reading PDFs"),
                    ):
                        self.assertEqual(page_repair.repair_document_pages(document_id)["status"], "skipped_active")
            finally:
                with self.connect() as conn:
                    conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))

    def test_page_repair_scope_does_not_touch_another_knowledge_base(self):
        from app import page_repair

        with patch.object(page_repair, "get_conn", side_effect=self.connect), patch.object(
            page_repair, "repair_document_pages", return_value={"status": "preview"},
        ) as repair:
            page_repair.repair_pages(knowledge_base_id=self.kb_b, dry_run=True)
            repair.assert_called_once_with(self.documents[1], dry_run=True)
            for arguments in [{}, {"knowledge_base_id": 0}, {"document_id": -1},
                              {"knowledge_base_id": self.kb_a, "document_id": self.documents[0]}]:
                with self.assertRaises(ValueError):
                    page_repair.repair_pages(**arguments)

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
