"""UI smoke test; all HTTP responses are controlled in process."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app.entity_types import TYPE_DEFINITIONS


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Install frontend requirements for UI tests')
class FrontendTests(unittest.TestCase):
    def test_new_types_question_and_maintenance_are_usable(self):
        from streamlit.testing.v1 import AppTest
        def response(payload):
            result=Mock()
            result.json.return_value=payload
            return result
        def get(url,params=None,**kwargs):
            path=url.split(':8080')[-1]
            if path=='/knowledge-bases':
                return response({'knowledge_bases':[{'id':1,'name':'Controlled KB','document_count':1}]})
            if path=='/entity-types':
                return response({'types':TYPE_DEFINITIONS})
            if '/maintenance/' in path:
                return response({'status':'done','result':[]})
            resource=path.rsplit('/',1)[-1]
            if resource=='graph':
                return response({'nodes':[],'edges':[],'summary':{}})
            return response({{'browse':'pages'}.get(resource,resource):[]})
        def post(url,json=None,**kwargs):
            if url.endswith('/ask'):
                return response({'status':'evidence_only','answer':'Controlled evidence answer.','citations':[],'warnings':[]})
            if url.endswith('/maintenance'):
                return response({'task_id':'controlled','status_url':'/knowledge-bases/1/maintenance/controlled'})
            raise AssertionError(url)
        with patch('requests.get',side_effect=get), patch('requests.post',side_effect=post) as send:
            app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'frontend/app.py')).run(timeout=20)
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            choices=next(x for x in app.selectbox if x.label=='按实体类型筛选')
            self.assertIn('CellType',choices.options)
            next(x for x in app.text_area if x.label=='向当前知识库提问').set_value('Controlled question')
            next(b for b in app.button if b.label=='检索证据并回答').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertTrue(any('Controlled evidence answer' in x.value for x in app.markdown))
            next(b for b in app.button if b.label=='开始维护').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertEqual(send.call_args.kwargs['json']['mode'],'derived')
