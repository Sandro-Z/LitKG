"""Batch synchronization retry/rollback and parser error attribution."""
import os
from contextlib import nullcontext
import unittest
from unittest.mock import MagicMock, patch

from psycopg.errors import DeadlockDetected


class BatchStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import batch_status, tasks
        cls.batch, cls.tasks = batch_status, tasks

    def test_worker_deadlock_rolls_back_entire_transaction_before_sorted_retry(self):
        contexts = [MagicMock(), MagicMock()]
        cursors = [c.__enter__.return_value.cursor.return_value.__enter__.return_value for c in contexts]
        for cur in cursors:
            cur.fetchall.return_value = [{'batch_id':23}, {'batch_id':22}]
        calls = []
        def sync(cur, batch_id):
            calls.append((cur, batch_id))
            if len(calls) == 1:
                raise DeadlockDetected('batch22 replay')
        with patch.object(self.batch, 'get_conn', side_effect=contexts), \
             patch.object(self.batch, '_sync_batch_status', side_effect=sync), patch.object(self.batch.time, 'sleep'):
            self.batch.sync_batches_for_document(118)
        self.assertEqual([bid for _,bid in calls], [22,22,23])
        self.assertIs(contexts[0].__exit__.call_args.args[0], DeadlockDetected)
        self.assertIsNot(calls[0][0], calls[1][0])
        self.assertIsNone(contexts[1].__exit__.call_args.args[0])

    def test_retry_is_finite_and_api_savepoint_rolls_back_before_retry(self):
        cur = MagicMock()
        with patch.object(self.batch, '_sync_batch_status', side_effect=DeadlockDetected('controlled')) as sync, \
             patch.object(self.batch.time, 'sleep'):
            with self.assertRaises(DeadlockDetected):
                self.batch.sync_batch_status(cur, 22)
        self.assertEqual(sync.call_count, 3)
        self.assertEqual(cur.connection.transaction.call_count, 3)
        exits = cur.connection.transaction.return_value.__exit__.call_args_list
        self.assertTrue(all(c.args[0] is DeadlockDetected for c in exits))

    def test_batch_snapshot_failure_cannot_fail_document118_before_pdf_parser(self):
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = {'id':118, 'source_path':'/test/118.pdf'}
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        with patch.object(self.tasks, 'get_conn', return_value=conn), \
             patch.object(self.tasks, 'processing_lock', return_value=nullcontext(True)), \
             patch.object(self.tasks, 'sync_batches_for_document', side_effect=DeadlockDetected('batch22 replay')), \
             patch.object(self.tasks, 'archive_claims'), patch.object(self.tasks, 'lock_graph_projection'), \
             patch.object(self.tasks.log, 'exception') as logged, \
             patch.object(self.tasks, 'parse_pdf_to_chunks', return_value=({}, [{'text':'Original PDF text.'}])) as parser:
            result = self.tasks.parse_document.run(118, publish=False)
        parser.assert_called_once_with('/test/118.pdf')
        self.assertEqual(result['status'], 'cataloging')
        logged.assert_called_once()
        self.assertFalse(any('Parse failed' in str(c) for c in cur.execute.call_args_list))
