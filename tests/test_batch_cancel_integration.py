"""Cancellation races and resumed work against disposable PostgreSQL, controlled models."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from psycopg.types.json import Jsonb
from app.schemas import CatalogBundle, ClaimBundle
from app.ingestion import StoredDocument, register_document
from pdf_fixtures import text_pdf
import test_knowledge_integration as fixtures


@unittest.skipUnless(os.getenv('LITKG_TEST_DATABASE_URL'), 'Set disposable LITKG_TEST_DATABASE_URL')
class BatchCancelIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.KnowledgeIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.KnowledgeIntegrationTests.tearDownClass.__func__)
    connect = classmethod(fixtures.KnowledgeIntegrationTests.connect.__func__)
    setUp = fixtures.KnowledgeIntegrationTests.setUp
    new_kb = fixtures.KnowledgeIntegrationTests.new_kb
    new_doc = fixtures.KnowledgeIntegrationTests.new_doc
    chunk = fixtures.KnowledgeIntegrationTests.chunk
    store = fixtures.KnowledgeIntegrationTests.store
    maintenance_job = fixtures.KnowledgeIntegrationTests.maintenance_job

    def batch(self, docs=None, status='queued', item_status='queued'):
        with self.connect() as conn:
            batch = conn.execute('INSERT INTO ingestion_batches (knowledge_base_id,status) VALUES (%s,%s) RETURNING id',
                                 (self.kb,status)).fetchone()['id']
            for pos, doc in enumerate(docs or [self.doc]):
                conn.execute("INSERT INTO ingestion_batch_items (batch_id,position,filename,document_id,status) VALUES (%s,%s,'test.pdf',%s,%s)",
                             (batch,pos,doc,item_status))
            return batch

    def active(self, status='queued', catalog_status='pending'):
        with self.connect() as conn:
            conn.execute('UPDATE documents SET status=%s,catalog_status=%s WHERE id=%s', (status,catalog_status,self.doc))

    def cancel(self, batch):
        response = self.client.post(f'/ingestion-batches/{batch}/cancel')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def state(self):
        with self.connect() as conn:
            return conn.execute('SELECT * FROM documents WHERE id=%s', (self.doc,)).fetchone()

    def test_cancellation_is_idempotent_preserves_results_cache_and_frozen_snapshot(self):
        sentence = 'Probe Q reduces IL-6.'
        chunk = self.chunk(self.doc, sentence)
        claim_id = self.store(self.doc, chunk, sentence)[0]
        self.active()
        batch = self.batch()
        with self.connect() as conn:
            conn.execute("INSERT INTO processing_cache (document_id,stage,cache_key,payload) VALUES (%s,'catalog','kept',%s)",
                         (self.doc,Jsonb({'entities':[]})))
        result = self.cancel(batch)
        self.assertEqual(result['stopped_document_ids'], [self.doc])
        self.assertEqual(result['continued_document_ids'], [])
        generation = self.state()['pipeline_generation']
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assertTrue(self.cancel(batch)['already_cancelled'])
        self.assertEqual(self.state()['pipeline_generation'], generation)
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM claims WHERE id=%s', (claim_id,)).fetchone())
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM processing_cache WHERE document_id=%s', (self.doc,)).fetchone()['n'], 1)
            self.assertEqual(conn.execute('SELECT status FROM chunks WHERE id=%s', (chunk,)).fetchone()['status'], 'cancelled')
            conn.execute("UPDATE documents SET status='done' WHERE id=%s", (self.doc,))
        payload = self.client.get(f'/ingestion-batches/{batch}').json()
        self.assertEqual(payload['status'], 'cancelled')
        self.assertEqual(payload['items'][0]['status'], 'cancelled')
        self.tasks.update_document_status(self.doc, 'done')
        # A cancelled document cannot be relabelled by an out-of-scope publisher.
        with self.connect() as conn:
            conn.execute("UPDATE documents SET status='cancelled' WHERE id=%s", (self.doc,))
        self.tasks.update_document_status(self.doc, 'failed', 'late publisher')
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assertEqual(self.tasks.refresh_document_status(self.doc), 'cancelled')

    def test_api_missing_finished_and_repeated_cancel(self):
        self.assertEqual(self.client.post('/ingestion-batches/99999999/cancel').status_code, 404)
        batch = self.batch(status='done', item_status='done')
        self.assertEqual(self.client.post(f'/ingestion-batches/{batch}/cancel').status_code, 409)
        self.assertEqual(self.client.get(f'/ingestion-batches/{batch}').json()['status'], 'done')

    def test_shared_active_document_continues_for_other_batch(self):
        self.active()
        first, second = self.batch(), self.batch()
        result = self.cancel(first)
        self.assertEqual(result['continued_document_ids'], [self.doc])
        self.assertEqual(self.state()['status'], 'queued')
        self.assertEqual(self.state()['pipeline_generation'], 0)
        result = self.cancel(second)
        self.assertEqual(result['stopped_document_ids'], [self.doc])
        self.assertEqual(self.state()['status'], 'cancelled')
        for batch in [first,second]:
            self.assertEqual(self.client.get(f'/ingestion-batches/{batch}').json()['status'], 'cancelled')

    def test_concurrent_shared_batch_cancels_stop_last_consumer(self):
        self.active()
        batches = [self.batch(),self.batch()]
        barrier = Barrier(2)
        def cancel(batch):
            barrier.wait(timeout=10)
            return self.cancel(batch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(cancel, batches))
        self.assertEqual(sum(len(r['stopped_document_ids']) for r in results), 1)
        self.assertEqual(self.state()['status'], 'cancelled')
        self.assertEqual(self.state()['pipeline_generation'], 1)

    def test_completed_peer_batch_keeps_its_history(self):
        old = self.batch(status='done', item_status='done')
        self.active()
        current = self.batch()
        self.cancel(current)
        payload = self.client.get(f'/ingestion-batches/{old}').json()
        self.assertEqual(payload['status'], 'done')
        self.assertEqual(payload['items'][0]['status'], 'done')

    def test_independent_maintenance_keeps_processing_shared_document(self):
        self.active()
        batch = self.batch()
        self.maintenance_job()
        self.assertEqual(self.cancel(batch)['continued_document_ids'], [self.doc])
        self.assertEqual(self.state()['status'], 'queued')

    def test_queued_tasks_skip_and_stale_generation_cannot_resume(self):
        chunk = self.chunk(self.doc, 'Probe Q reduces IL-6.')
        self.active()
        self.cancel(self.batch())
        with patch.object(self.tasks, 'parse_pdf_to_chunks', side_effect=AssertionError('cancelled parser')), \
             patch.object(self.tasks, 'extract_catalog', side_effect=AssertionError('cancelled catalog')), \
             patch.object(self.tasks, 'extract_claims', side_effect=AssertionError('cancelled claims')):
            self.assertEqual(self.tasks.parse_document.run(self.doc)['status'], 'cancelled')
            self.assertEqual(self.tasks.build_document_catalog.run(self.doc)['status'], 'cancelled')
            self.assertEqual(self.tasks.extract_chunk.run(chunk)['status'], 'cancelled')
            with self.connect() as conn:
                conn.execute("UPDATE documents SET status='queued' WHERE id=%s", (self.doc,))
            self.assertEqual(self.tasks.parse_document.run(self.doc, generation=0)['status'], 'superseded')
            self.assertEqual(self.tasks.build_document_catalog.run(self.doc, generation=0)['status'], 'superseded')
            self.assertEqual(self.tasks.extract_chunk.run(chunk, generation=0)['status'], 'superseded')

    def test_reupload_explicitly_reopens_document_but_preserves_cancelled_batch(self):
        self.active()
        batch = self.batch()
        self.cancel(batch)
        before = self.state()
        with self.connect() as conn:
            registered = register_document(conn.cursor(), stored=StoredDocument(filename='again.pdf',
                sha256=before['sha256'],source_path=before['source_path'],size_bytes=10), force_reprocess=False)
        self.assertTrue(registered.queued)
        self.assertEqual(self.state()['status'], 'queued')
        self.assertGreater(self.state()['pipeline_generation'], before['pipeline_generation'])
        self.assertEqual(self.client.get(f'/ingestion-batches/{batch}').json()['status'], 'cancelled')

    def test_cancellation_during_upload_prevents_more_registrations_and_publication(self):
        original = self.main.store_pdf
        batches, stored = [], []
        def store(*args,**kwargs):
            result = original(*args,**kwargs)
            stored.append(result)
            if len(stored) == 2:
                with self.connect() as conn:
                    batch = conn.execute('SELECT id FROM ingestion_batches WHERE knowledge_base_id=%s ORDER BY id DESC LIMIT 1',
                                         (self.kb,)).fetchone()['id']
                batches.append(batch)
                self.cancel(batch)
            return result
        with text_pdf(['Controlled upload source.']) as pdf, tempfile.TemporaryDirectory() as uploads:
            content = pdf.read_bytes()
            with patch.object(self.main, 'UPLOAD_DIR', Path(uploads)), \
                 patch.object(self.main, 'store_pdf', side_effect=store), \
                 patch.object(self.tasks.parse_document, 'apply_async') as publish:
                response = self.client.post(f'/knowledge-bases/{self.kb}/documents', files=[
                    ('files',('first.pdf',content,'application/pdf')),('files',('second.pdf',content,'application/pdf'))])
        self.assertEqual(response.status_code, 409, response.text)
        publish.assert_not_called()
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM ingestion_batch_items WHERE batch_id=%s', (batches[0],)).fetchone()['n'], 1)
        self.assertEqual(self.client.get(f'/ingestion-batches/{batches[0]}').json()['status'], 'cancelled')

    def test_failed_document_in_mixed_batch_cannot_restart_from_old_delivery(self):
        other = self.new_doc(self.kb)
        self.active('failed')
        with self.connect() as conn:
            conn.execute("UPDATE documents SET status='queued' WHERE id=%s", (other,))
        batch = self.batch(docs=[self.doc,other])
        self.cancel(batch)
        with patch.object(self.tasks, 'extract_catalog', side_effect=AssertionError('cancelled redelivery')):
            self.assertEqual(self.tasks.build_document_catalog.run(self.doc)['status'], 'cancelled')

    def test_cancel_during_parser_stops_writes_and_catalog_publication(self):
        self.active()
        batch = self.batch()
        def parser(path):
            self.cancel(batch)
            return {}, [{'text':'Probe Q reduces IL-6.'}]
        with patch.object(self.tasks, 'parse_pdf_to_chunks', side_effect=parser), \
             patch.object(self.tasks.build_document_catalog, 'apply_async') as publish:
            result = self.tasks.parse_document.run(self.doc)
        self.assertEqual(result['status'], 'cancelled')
        publish.assert_not_called()
        self.assertEqual(self.state()['status'], 'cancelled')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM chunks WHERE document_id=%s', (self.doc,)).fetchone()['n'], 0)

    def test_cancel_during_catalog_retains_successful_siblings_and_stops_publication(self):
        sentence = 'Probe Q reduces IL-6.'
        self.chunk(self.doc, sentence)
        self.chunk(self.doc, sentence + ' Second result.', index=1)
        self.active()
        batch = self.batch()
        calls = []
        def model(heading,text,**kwargs):
            calls.append(text)
            if len(calls) == 2:
                self.cancel(batch)
            return CatalogBundle.model_validate({'entities':[{'text':'Probe Q','type':'Drug','mention':'Probe Q','evidence':sentence}]})
        with patch.object(self.tasks, 'extract_catalog', side_effect=model), \
             patch.object(self.tasks.extract_chunk, 'apply_async') as publish:
            result = self.tasks.build_document_catalog.run(self.doc)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(calls), 2)
        publish.assert_not_called()
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM processing_cache WHERE document_id=%s', (self.doc,)).fetchone()['n'], 1)
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM document_entities WHERE document_id=%s', (self.doc,)).fetchone()['n'], 0)
        self.assertEqual(self.state()['catalog_status'], 'cancelled')

    def test_cancel_during_claim_model_preserves_existing_claims_and_skips_failure_gap(self):
        sentence = 'Probe Q reduces IL-6.'
        chunk = self.chunk(self.doc, sentence)
        claim_id = self.store(self.doc, chunk, sentence)[0]
        self.active('extracting', 'done')
        with self.connect() as conn:
            conn.execute("UPDATE documents SET catalog_version='test' WHERE id=%s", (self.doc,))
        batch = self.batch(item_status='extracting')
        def model(*args,**kwargs):
            self.cancel(batch)
            return ClaimBundle(claims=[])
        with patch.object(self.tasks, 'extract_claims', side_effect=model):
            result = self.tasks.extract_chunk.run(chunk)
        self.assertEqual(result['status'], 'cancelled')
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM claims WHERE id=%s', (claim_id,)).fetchone())
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM processing_attempts WHERE document_id=%s', (self.doc,)).fetchone()['n'], 0)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM review_items WHERE document_id=%s AND kind='processing_gap'", (self.doc,)).fetchone()['n'], 0)

    def test_cancel_during_transient_http_failure_prevents_another_model_request(self):
        from openai import APIConnectionError
        from app import llm
        self.chunk(self.doc, 'Probe Q reduces IL-6.')
        self.active()
        batch = self.batch()
        def request(**kwargs):
            self.cancel(batch)
            raise APIConnectionError(message='Controlled connection failure', request=Mock())
        with patch.object(llm.client.chat.completions, 'create', side_effect=request) as model, \
             patch.object(llm.chat_completion.retry, 'sleep'):
            result = self.tasks.build_document_catalog.run(self.doc)
        self.assertEqual(result['status'], 'cancelled')
        model.assert_called_once()
        self.assertIsNone(llm.model_request_check.get())

    def test_whole_kb_retry_skips_cancelled_but_explicit_document_retry_resumes(self):
        self.chunk(self.doc, 'Probe Q reduces IL-6.')
        self.active('extracting', 'done')
        batch = self.batch()
        self.cancel(batch)
        job = self.maintenance_job()
        with self.connect() as conn:
            conn.execute('UPDATE maintenance_jobs SET document_id=NULL WHERE id=%s', (job,))
        with patch.object(self.tasks, 'extract_catalog', side_effect=AssertionError('must skip')), \
             patch.object(self.tasks, 'extract_claims', side_effect=AssertionError('must skip')):
            result = self.tasks.maintain_knowledge_base.run(job)
        self.assertEqual(result['documents'][0]['status'], 'cancelled')
        self.assertEqual(self.state()['status'], 'cancelled')
        job = self.maintenance_job()
        with patch.object(self.tasks, 'extract_claims', return_value=ClaimBundle(claims=[])):
            self.tasks.maintain_knowledge_base.run(job)
        self.assertEqual(self.state()['status'], 'done')
        self.assertEqual(self.client.get(f'/ingestion-batches/{batch}').json()['status'], 'cancelled')
