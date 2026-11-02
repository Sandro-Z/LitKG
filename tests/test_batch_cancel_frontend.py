"""Batch cancellation UI with controlled HTTP responses."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from app.entity_types import TYPE_DEFINITIONS


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Install frontend requirements for UI tests')
class BatchCancelFrontendTests(unittest.TestCase):
    def test_cancel_selected_batch_and_show_persistent_cancelled_state(self):
        from streamlit.testing.v1 import AppTest
        state = {'status':'processing'}
        def response(payload):
            result = Mock()
            result.json.return_value = payload
            return result
        def get(url,params=None,**kwargs):
            path = url.split(':8080')[-1]
            if path == '/knowledge-bases':
                return response({'knowledge_bases':[{'id':1,'name':'Controlled KB','document_count':1}]})
            if path == '/entity-types':
                return response({'types':TYPE_DEFINITIONS})
            if path.endswith('/batches'):
                return response({'batches':[{'id':9,'status':state['status']}]})
            if path == '/ingestion-batches/9':
                return response({'status':state['status'],'accepted_count':1,
                    'items':[{'document_id':1,'status':state['status']}]})
            resource = path.rsplit('/',1)[-1]
            if resource == 'graph':
                return response({'nodes':[],'edges':[],'summary':{}})
            return response({{'browse':'pages'}.get(resource,resource):[]})
        def post(url,json=None,**kwargs):
            self.assertTrue(url.endswith('/ingestion-batches/9/cancel'))
            state['status'] = 'cancelled'
            return response({'status':'cancelled','stopped_document_ids':[1],'continued_document_ids':[]})
        with patch('requests.get', side_effect=get), patch('requests.post', side_effect=post) as send:
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'frontend/app.py')).run(timeout=20)
            self.assertFalse(app.exception)
            next(button for button in app.button if button.label == '取消批次').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(next(metric.value for metric in app.metric if metric.label == '批次状态'), 'cancelled')
            self.assertFalse(any(button.label == '取消批次' for button in app.button))
            self.assertTrue(any('本批次已取消' in notice.value for notice in app.info))
            self.assertEqual(next(metric.value for metric in app.metric if metric.label == '已结束比例'), '100.0%')
