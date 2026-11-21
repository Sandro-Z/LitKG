"""Plain-text document import: storage, parsing, dispatch, API and export."""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.ingestion import (RegisteredDocument, UploadRejected, canonical_text_document, is_text_source,
                           store_text_document)
from app.parser import parse_text_source


def document(text='Aspirin reduces colon cancer risk. Aspirin lowered PTGS2.', **extra):
    return {'id': 'biored:111', 'external_id': 'biored:111', 'title': 'Aspirin reduces colon cancer risk.',
            'parsing_status': 'complete', 'blocks': [{'id': 'ta', 'section': 'title+abstract', 'text': text}], **extra}


class TextStorageTests(unittest.TestCase):
    def test_identical_documents_are_content_addressed_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            first = store_text_document(document(), Path(directory), 1 << 20)
            # Unused fields (parsing_status, ordering) do not change identity.
            second = store_text_document(dict(reversed(list(document().items()))), Path(directory), 1 << 20)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.source_path, second.source_path)
            self.assertTrue(is_text_source(first.source_path))
            self.assertEqual(len(list(Path(directory).glob('*.litkg-text.json'))), 1)
            self.assertEqual((first.title, first.external_id), ('Aspirin reduces colon cancer risk.', 'biored:111'))
            stored = json.loads(Path(first.source_path).read_text())
            self.assertEqual(stored['blocks'][0]['text'], document()['blocks'][0]['text'])
            changed = store_text_document(document(text='Different text.'), Path(directory), 1 << 20)
            self.assertNotEqual(changed.sha256, first.sha256)

    def test_invalid_documents_are_rejected(self):
        cases = [[], {'blocks': []}, document(blocks=[]), document(blocks=[{'id': 'a', 'text': ' '}]),
                 document(blocks=[{'id': 'a', 'text': 'x'}, {'id': 'a', 'text': 'y'}]),
                 {**document(), 'id': ''}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(UploadRejected):
                canonical_text_document(value)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(UploadRejected):
            store_text_document(document(), Path(directory), 10)

    def test_external_id_defaults_to_document_id(self):
        value = document()
        value.pop('external_id')
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(store_text_document(value, Path(directory), 1 << 20).external_id, 'biored:111')


class TextParsingTests(unittest.TestCase):
    def stored(self, directory, text):
        return store_text_document(document(text=text), Path(directory), 1 << 24).source_path

    def test_one_block_becomes_one_chunk_with_source_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, chunks = parse_text_source(self.stored(directory, 'Aspirin lowered PTGS2.'))
        self.assertEqual(metadata, {'title': 'Aspirin reduces colon cancer risk.', 'doi': None})
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0]['source_block_id'], chunks[0]['source_offset']), ('ta', 0))
        self.assertEqual(chunks[0]['text'], 'Aspirin lowered PTGS2.')
        self.assertEqual(chunks[0]['section_title'], 'title+abstract')
        self.assertIn('input_issues', chunks[0])

    def test_long_block_reuses_the_splitter_and_offsets_reproduce_text(self):
        text = ' '.join(f'Sentence {i} reports that compound {i} lowers marker {i}.' for i in range(900))
        with tempfile.TemporaryDirectory() as directory:
            _, chunks = parse_text_source(self.stored(directory, text))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            offset = chunk['source_offset']
            self.assertEqual(text[offset:offset + len(chunk['text'])], chunk['text'])
        self.assertEqual([c['source_offset'] for c in chunks], sorted(c['source_offset'] for c in chunks))

    def test_parse_sources_dispatches_text_without_grobid_or_vlm(self):
        from app import vlm
        pdf_parser = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            path = self.stored(directory, 'Aspirin lowered PTGS2.')
            with patch.object(vlm, 'enabled', return_value=True), patch.object(vlm, 'image_chunks') as images:
                _, chunks = vlm.parse_sources(path, pdf_parser)
        pdf_parser.assert_not_called()
        images.assert_not_called()
        self.assertEqual(len(chunks), 1)


class ParseDocumentTextSourceTests(unittest.TestCase):
    def test_parse_document_stores_offsets_and_queues_catalog(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import tasks
        with tempfile.TemporaryDirectory() as directory:
            path = store_text_document(document(), Path(directory), 1 << 20).source_path
            row = {'id': 5, 'source_path': path, 'pipeline_generation': 0}
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = row
            context = MagicMock()
            context.__enter__.return_value = conn
            cursor = conn.cursor.return_value.__enter__.return_value

            @contextmanager
            def lock(_key):
                yield True

            token = tasks._processing_scope.set((5, 0))
            try:
                with patch.object(tasks, 'processing_lock', lock), patch.object(tasks, 'get_conn', return_value=context), \
                        patch.object(tasks, 'update_document_status'), patch.object(tasks, 'check_processing'), \
                        patch.object(tasks, 'lock_graph_projection'), patch.object(tasks, 'archive_claims'), \
                        patch.object(tasks, 'parse_pdf_to_chunks') as pdf_parser, \
                        patch.object(tasks.build_document_catalog, 'apply_async') as enqueue:
                    result = tasks.parse_document.run.__wrapped__(5)
            finally:
                tasks._processing_scope.reset(token)
        pdf_parser.assert_not_called()
        self.assertEqual(result, {'document_id': 5, 'chunk_count': 1, 'status': 'cataloging'})
        enqueue.assert_called_once()
        inserts = [c for c in cursor.execute.call_args_list if 'INSERT INTO chunks' in c.args[0]]
        self.assertEqual(len(inserts), 1)
        params = inserts[0].args[1]
        self.assertEqual(params[-2:], ('ta', 0))
        self.assertEqual(params[6], document()['blocks'][0]['text'])
        title_update = next(c for c in cursor.execute.call_args_list if 'UPDATE documents SET title' in c.args[0])
        self.assertEqual(title_update.args[1][0], 'Aspirin reduces colon cancer risk.')


class TextDocumentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        cls.upload = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {**TEST_ENV, 'UPLOAD_DIR': cls.upload.name}):
            from app import main
        cls.main = main
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.upload.cleanup()

    def post(self, payload, registered=None):
        cursor = MagicMock()
        cursor.fetchone.side_effect = lambda: {'id': 11, 'status': 'queued'}
        context = MagicMock()
        connection = context.__enter__.return_value
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.execute.return_value.fetchone.return_value = {'status': 'queued', 'pipeline_generation': 1}
        registered = registered or RegisteredDocument(document_id=42, status='queued', inserted=True, queued=True)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.main, 'UPLOAD_DIR', Path(directory)), \
                patch.object(self.main, 'get_conn', return_value=context), \
                patch.object(self.main, 'register_document', return_value=registered) as register, \
                patch.object(self.main.parse_document, 'apply_async') as parse, \
                patch.object(self.main, '_batch_payload', side_effect=lambda batch_id, item_metadata=None:
                             {'id': batch_id, 'status': 'queued', 'items': [], 'meta': item_metadata}):
            response = self.client.post('/knowledge-bases/3/text-documents?batch_name=biored-test', json=payload)
            files = sorted(p.name for p in Path(directory).iterdir())
        return response, register, parse, files

    def test_text_documents_are_registered_and_queued_for_parsing(self):
        response, register, parse, files = self.post([document(), document(text='Second abstract.')])
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(register.call_count, 2)
        stored = register.call_args_list[0].kwargs['stored']
        self.assertTrue(stored.source_path.endswith('.litkg-text.json'))
        self.assertEqual((stored.title, stored.external_id), ('Aspirin reduces colon cancer risk.', 'biored:111'))
        self.assertEqual(len(files), 2)
        parse.assert_called_once_with(args=[42], kwargs={'generation': 1}, queue='parse')

    def test_duplicate_documents_share_one_source_file(self):
        response, register, _, files = self.post([document(), document()])
        self.assertEqual(response.status_code, 202, response.text)
        first, second = (c.kwargs['stored'] for c in register.call_args_list)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(files), 1)

    def test_invalid_document_is_rejected_per_item_and_limits_apply(self):
        response, register, parse, _ = self.post([{'id': 'x', 'blocks': []}])
        self.assertEqual(response.status_code, 202)
        register.assert_not_called()
        parse.assert_not_called()
        self.assertEqual(response.json()['meta']['0']['queued_for_processing'], False)
        with patch.object(self.main, 'MAX_TEXT_DOCUMENTS', 1):
            response, *_ = self.post([document(), document()])
        self.assertEqual(response.status_code, 400)
        response, *_ = self.post([])
        self.assertEqual(response.status_code, 400)


class TextQualityExportTests(unittest.TestCase):
    def test_export_adds_external_id_and_block_offsets_only_for_text_sources(self):
        from app import quality_export
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [{'id': 1, 'title': 'T', 'sha256': 'a' * 64, 'status': 'done', 'pipeline_generation': 2,
              'external_id': 'biored:1', 'is_text_source': True},
             {'id': 2, 'title': 'P', 'sha256': 'b' * 64, 'status': 'done', 'pipeline_generation': 1,
              'external_id': None, 'is_text_source': False}],
            [{'id': 10, 'chunk_index': 0, 'section_title': 'ta', 'page_start': None, 'page_end': None,
              'page_spans': [], 'text': 'abc', 'source_block_id': 'ta', 'source_offset': 0}],
            [{'id': 20, 'chunk_index': 0, 'section_title': 'Page 1', 'page_start': 1, 'page_end': 1,
              'page_spans': [], 'text': 'pdf', 'source_block_id': None, 'source_offset': None}],
        ]
        with patch.object(quality_export, 'collect_claims', return_value={'claims': [], 'exported_count': 0}):
            snapshot = quality_export.collect_quality_snapshot(cursor, 3)
        text_doc, pdf_doc = snapshot['documents']
        self.assertEqual((text_doc['external_id'], text_doc['source_kind']), ('biored:1', 'text'))
        self.assertEqual((text_doc['blocks'][0]['source_block_id'], text_doc['blocks'][0]['source_offset']), ('ta', 0))
        self.assertNotIn('external_id', pdf_doc)
        self.assertNotIn('source_kind', pdf_doc)
        self.assertNotIn('source_block_id', pdf_doc['blocks'][0])


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'), 'Set LITKG_TEST_DATABASE_URL to a disposable PostgreSQL database')
class TextImportDatabaseTests(unittest.TestCase):
    """Text source -> chunks -> catalog -> extraction -> quality export, on a real schema."""

    @classmethod
    def setUpClass(cls):
        import uuid
        import psycopg
        from psycopg import sql
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import tasks
        cls.tasks, cls.psycopg, cls.sql = tasks, psycopg, sql
        cls.dsn = os.environ['LITKG_TEST_DATABASE_URL']
        cls.schema = 'litkg_text_' + uuid.uuid4().hex
        migration = (Path(__file__).resolve().parents[1] / 'db/init.sql').read_text()
        with psycopg.connect(cls.dsn) as conn:
            conn.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
            conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        with cls.connect() as conn:
            conn.execute(migration)

    @classmethod
    def connect(cls):
        from psycopg.rows import dict_row
        return cls.psycopg.connect(cls.dsn, row_factory=dict_row, options=f'-csearch_path={cls.schema},public')

    @classmethod
    def tearDownClass(cls):
        with cls.psycopg.connect(cls.dsn) as conn:
            conn.execute(cls.sql.SQL('DROP SCHEMA {} CASCADE').format(cls.sql.Identifier(cls.schema)))

    def test_text_document_runs_catalog_and_extraction_and_exports_offsets(self):
        from app.ingestion import register_document
        from app.quality_export import collect_quality_snapshot
        from app.schemas import CatalogBundle, ClaimBundle
        tasks = self.tasks
        sentence = 'Gefitinib inhibits EGFR.'
        with tempfile.TemporaryDirectory() as directory:
            stored = store_text_document(document(text=sentence), Path(directory), 1 << 20)
            with self.connect() as conn:
                kb = conn.execute("INSERT INTO knowledge_bases (slug,name) VALUES ('text','Text') RETURNING id").fetchone()['id']
                registered = register_document(conn.cursor(), stored=stored, force_reprocess=False)
                again = register_document(conn.cursor(), stored=stored, force_reprocess=False)
                conn.execute('INSERT INTO knowledge_base_documents (knowledge_base_id, document_id) VALUES (%s,%s)',
                             (kb, registered.document_id))
            self.assertTrue(registered.inserted)
            self.assertEqual((again.document_id, again.inserted), (registered.document_id, False))
            doc_id = registered.document_id
            with patch.object(tasks, 'get_conn', side_effect=self.connect), \
                    patch.object(tasks, 'parse_pdf_to_chunks') as pdf_parser, \
                    patch.object(tasks.build_document_catalog, 'apply_async') as enqueue:
                result = tasks.parse_document.run(doc_id)
            pdf_parser.assert_not_called()
            self.assertEqual(result['chunk_count'], 1)
            enqueue.assert_called_once()
            catalog = CatalogBundle.model_validate({'entities': [
                {'text': 'Gefitinib', 'type': 'Drug', 'evidence': sentence},
                {'text': 'EGFR', 'type': 'Protein', 'evidence': sentence}]})
            with patch.object(tasks, 'get_conn', side_effect=self.connect), \
                    patch.object(tasks, 'extract_catalog', return_value=catalog), \
                    patch.object(tasks.extract_chunk, 'apply_async') as relationships:
                tasks.build_document_catalog.run(doc_id)
            chunk_id = relationships.call_args.kwargs['args'][0]
            bundle = ClaimBundle.model_validate({'claims': [{
                'subject': {'text': 'Gefitinib', 'type': 'Drug'}, 'predicate': 'INHIBITS',
                'object': {'text': 'EGFR', 'type': 'Protein', 'process': {'kind': 'activity', 'direction': 'decreased'}},
                'evidence': {'sentence': sentence}, 'confidence': 0.9}]})
            with patch.object(tasks, 'get_conn', side_effect=self.connect), \
                    patch.object(tasks, 'extract_claims', return_value=bundle), \
                    patch.object(tasks, 'refresh_document_status'):
                tasks.extract_chunk.run(chunk_id)
            with self.connect() as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                with conn.cursor() as cur:
                    snapshot = collect_quality_snapshot(cur, kb)
        exported = snapshot['documents'][0]
        self.assertEqual((exported['external_id'], exported['source_kind'], exported['title']),
                         ('biored:111', 'text', 'Aspirin reduces colon cancer risk.'))
        self.assertEqual((exported['blocks'][0]['source_block_id'], exported['blocks'][0]['source_offset']), ('ta', 0))
        self.assertEqual(len(snapshot['claims']), 1)
