"""Exercise real Streamlit forms with an in-process, stateful HTTP fixture."""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from app.entity_types import TYPE_DEFINITIONS


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Install frontend requirements')
class KnowledgeBaseFrontendTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        self.kbs = [{'id': 1, 'name': '原始库', 'description': '说明', 'document_count': 0},
                    {'id': 10, 'name': '其他库', 'description': None, 'document_count': 0}]
        self.fail = None
        self.deleted = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('requests.get', side_effect=self.get))
        self.post = stack.enter_context(patch('requests.post', side_effect=self.create))
        self.put = stack.enter_context(patch('requests.put', side_effect=self.update))
        self.delete = stack.enter_context(patch('requests.delete', side_effect=self.remove))
        self.app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'frontend/app.py')).run(timeout=20)
        self.assertFalse(self.app.exception)

    def response(self, payload):
        response = Mock()
        response.json.return_value = payload
        return response

    def get(self, url, **kwargs):
        path = url.split(':8080')[-1]
        if path == '/knowledge-bases':
            return self.response({'knowledge_bases': [dict(kb) for kb in self.kbs]})
        if path == '/entity-types':
            return self.response({'types': TYPE_DEFINITIONS})
        if path.startswith('/knowledge-bases/'):
            self.assertNotIn(int(path.split('/')[2]), self.deleted)
        resource = path.rsplit('/', 1)[-1]
        if resource == 'graph':
            return self.response({'nodes': [], 'edges': [], 'summary': {}})
        return self.response({{'browse': 'pages'}.get(resource, resource): []})

    def check_failure(self, operation):
        if self.fail == operation:
            raise requests.HTTPError('controlled failure', response=self.response({'detail': '受控接口失败'}))

    def create(self, url, json, **kwargs):
        self.check_failure('create')
        self.assertTrue(url.endswith('/knowledge-bases'))
        created = {'id': 20, 'document_count': 0, **json}
        self.kbs.append(created)
        return self.response(created)

    def update(self, url, json, **kwargs):
        self.check_failure('update')
        kb = next(kb for kb in self.kbs if kb['id'] == int(url.rsplit('/', 1)[-1]))
        kb.update(json)
        return self.response(kb)

    def remove(self, url, params, **kwargs):
        self.check_failure('delete')
        kb = next(kb for kb in self.kbs if kb['id'] == int(url.rsplit('/', 1)[-1]))
        self.assertEqual(params['confirm_name'], kb['name'])
        self.kbs.remove(kb)
        self.deleted.append(kb['id'])
        return self.response({'status': 'deleted', 'knowledge_base_id': kb['id']})

    def click(self, label):
        next(button for button in self.app.button if button.label == label).click().run(timeout=20)
        self.assertFalse(self.app.exception)

    def open_create(self):
        self.click('新建知识库')
        self.app.text_input(key='new_knowledge_base_name').set_value(' 新库 ')
        self.app.text_area(key='new_knowledge_base_description').set_value(' 新说明 ')

    def test_create_success_clears_and_closes_form_selects_new_kb_and_survives_refresh(self):
        self.open_create()
        self.click('创建')
        self.assertTrue(any('已创建' in item.value for item in self.app.success))
        self.assertFalse(any(item.key == 'new_knowledge_base_name' for item in self.app.text_input))
        self.assertEqual(self.app.selectbox(key='selected_knowledge_base_id').value, 20)
        self.app.run(timeout=20)
        self.assertTrue(any('已创建' in item.value for item in self.app.success))
        self.click('新建知识库')
        self.assertEqual(self.app.text_input(key='new_knowledge_base_name').value, '')
        self.assertEqual(self.app.text_area(key='new_knowledge_base_description').value, '')
        self.click('创建')
        self.assertEqual(self.post.call_count, 1)
        self.assertTrue(any('请输入' in item.value for item in self.app.error))

    def test_create_failure_preserves_draft_for_retry(self):
        self.open_create()
        self.fail = 'create'
        self.click('创建')
        self.assertTrue(any('受控接口失败' in item.value for item in self.app.error))
        self.assertEqual(self.app.text_input(key='new_knowledge_base_name').value, ' 新库 ')
        self.assertFalse(self.app.success)
        self.fail = None
        self.click('创建')
        self.assertTrue(self.app.success)
        self.assertEqual(len(self.kbs), 3)

    def test_edit_saves_selected_kb_and_failure_preserves_input(self):
        self.app.text_input(key='edit_knowledge_base_name_1').set_value(' 修改库 ')
        self.app.text_area(key='edit_knowledge_base_description_1').set_value('')
        self.fail = 'update'
        self.click('保存修改')
        self.assertEqual(self.kbs[0]['name'], '原始库')
        self.assertEqual(self.app.text_input(key='edit_knowledge_base_name_1').value, ' 修改库 ')
        self.fail = None
        self.click('保存修改')
        self.assertEqual(self.kbs[0]['name'], '修改库')
        self.assertIsNone(self.kbs[0]['description'])
        self.assertEqual(self.kbs[1]['name'], '其他库')
        self.assertEqual(self.app.selectbox(key='selected_knowledge_base_id').value, 1)
        self.assertTrue(any('已更新' in item.value for item in self.app.success))

    def test_delete_requires_confirmation_then_switches_and_clears_only_deleted_cache(self):
        self.app.session_state['answer_1'] = {'answer': 'old'}
        self.app.session_state['answer_10'] = {'answer': 'keep'}
        self.click('确认删除整个知识库')
        self.delete.assert_not_called()
        self.assertTrue(any('名称不匹配' in item.value for item in self.app.error))
        self.app.text_input(key='delete_knowledge_base_name_1').set_value('原始库')
        self.click('确认删除整个知识库')
        self.assertEqual(self.app.selectbox(key='selected_knowledge_base_id').value, 10)
        self.assertNotIn('answer_1', self.app.session_state)
        self.assertEqual(self.app.session_state['answer_10'], {'answer': 'keep'})
        self.assertTrue(any('已删除' in item.value for item in self.app.success))
        self.assertFalse(self.app.error)

    def test_delete_failure_retains_selection_and_confirmation(self):
        self.app.text_input(key='delete_knowledge_base_name_1').set_value('原始库')
        self.fail = 'delete'
        self.click('确认删除整个知识库')
        self.assertEqual(self.app.selectbox(key='selected_knowledge_base_id').value, 1)
        self.assertEqual(self.app.text_input(key='delete_knowledge_base_name_1').value, '原始库')
        self.assertTrue(any('受控接口失败' in item.value for item in self.app.error))
        self.assertEqual(len(self.kbs), 2)

    def test_deleting_last_kb_keeps_success_and_allows_creating_again(self):
        self.kbs.pop()
        self.app.run(timeout=20)
        self.app.text_input(key='delete_knowledge_base_name_1').set_value('原始库')
        self.click('确认删除整个知识库')
        self.assertFalse(self.kbs)
        self.assertTrue(any('已删除' in item.value for item in self.app.success))
        self.open_create()
        self.click('创建')
        self.assertEqual(self.app.selectbox(key='selected_knowledge_base_id').value, 20)
