import os
import unittest
from unittest.mock import MagicMock, patch

from app import recovery


class RecoveryTests(unittest.TestCase):
    def test_broker_pending_unacked_and_priority_queues_all_protect_work(self):
        for index in range(14):
            values = [0]*14
            values[index] = 1
            client = MagicMock()
            client.pipeline.return_value.__enter__.return_value.execute.return_value = values
            with patch.dict(os.environ,{'REDIS_URL':'redis://localhost:1/15'}), patch.object(recovery.redis.Redis,'from_url',return_value=client):
                self.assertFalse(recovery.broker_is_idle())
            client.close.assert_called_once()

    def test_broker_unavailable_is_not_proof_of_a_lost_task(self):
        with patch.object(recovery.redis.Redis,'from_url',side_effect=ConnectionError('controlled')):
            self.assertFalse(recovery.broker_is_idle())

    def test_no_orphans_does_not_contact_broker(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        with patch.object(recovery,'broker_is_idle') as probe:
            self.assertEqual(recovery.reconcile_stalled(cur,batch_id=16),[])
        probe.assert_not_called()

    def test_live_lease_or_legacy_advisory_lock_prevents_finalizing(self):
        for lease, locked in [(True,True),(False,False)]:
            cur = MagicMock()
            cur.fetchall.side_effect = [[{'id':25}],[{'id':7}],[]]
            cur.fetchone.side_effect = [{'active':lease},{'acquired':locked}]
            with patch.object(recovery,'broker_is_idle',return_value=True):
                self.assertEqual(recovery.reconcile_stalled(cur,document_id=25),[])
            self.assertFalse(any('UPDATE documents' in str(call) for call in cur.execute.call_args_list))

    def test_idle_stale_document_finalizes_pending_chunks_and_old_maintenance(self):
        cur = MagicMock()
        cur.fetchall.side_effect = [[{'id':25}],[{'id':7}],[{'id':'old-job'}]]
        cur.fetchone.side_effect = [{'active':False},*([{'acquired':True}]*3)]
        with patch.object(recovery,'broker_is_idle',return_value=True):
            self.assertEqual(recovery.reconcile_stalled(cur,document_id=25),[25])
        sql = '\n'.join(call.args[0] for call in cur.execute.call_args_list)
        self.assertIn("UPDATE chunks SET status='failed'",sql)
        self.assertIn('UPDATE documents',sql)
        self.assertIn('UPDATE maintenance_jobs',sql)


if __name__ == '__main__':
    unittest.main()
