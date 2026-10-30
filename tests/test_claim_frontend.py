import importlib.util
from pathlib import Path
import unittest

HAS_STREAMLIT=importlib.util.find_spec('streamlit') is not None


@unittest.skipUnless(HAS_STREAMLIT,'Requires Streamlit testing dependencies')
class ClaimFrontendTests(unittest.TestCase):
    def app(self,documents=False):
        from streamlit.testing.v1 import AppTest
        path=Path(__file__).resolve().parents[1]/'frontend/app.py'
        base=f'''import importlib.util
import streamlit as st
spec=importlib.util.spec_from_file_location('litkg_front_ui',{str(path)!r})
front=importlib.util.module_from_spec(spec)
spec.loader.exec_module(front)
def post(path,body):
    if path.endswith('delete-preview'):
        return {{'count':600 if body['all_matching'] else len(body['claim_ids']),'document_ids':[1],'other_affected_knowledge_bases':[],'selection_hash':'f'*64}}
    if path.endswith('/delete'):
        st.session_state['deleted_body']=body
        return {{'deleted_count':600 if body['all_matching'] else len(body['claim_ids'])}}
    st.session_state['reprocess_body']=body
    return {{'task_id':'job-1','status_url':'/status'}}
front.api_post_json=post
'''
        if documents:
            base+='''front.api_get=lambda *args,**kwargs:{'jobs':[{'id':'job-a','mode':'reparse','document_id':None,'status':'cancelled'}, {'id':'job-b','mode':'reparse','document_id':1,'status':'queued'}]}
front.render_document_reprocessing(26,[{'id':1,'filename':'one.pdf'},{'id':2,'filename':'two.pdf'}])
'''
        else:
            base+='''page=st.session_state.get('fixture_page',1)
rows=[{'id':value,'subject_text':'FMT','predicate':'AFFECTS','object_text':'cells','evidence_text':'fixed evidence'} for value in ([1,2] if page==1 else [3,4])]
front.render_claim_selection(26,rows,{},600)
'''
        at=AppTest.from_string(base,default_timeout=20).run()
        self.assertFalse(at.exception,[e.message for e in at.exception])
        return at

    def button(self,at,label):
        return next(button for button in at.button if button.label==label)

    def test_all_matching_selection_is_not_limited_to_displayed_page(self):
        at=self.app()
        at.checkbox[0].check().run()
        self.assertFalse(at.exception,[e.message for e in at.exception])
        self.button(at,'删除选中的 600 条 claim').click().run()
        self.assertFalse(at.exception,[e.message for e in at.exception])
        self.assertTrue(at.session_state['deleted_body']['all_matching'])
        self.assertEqual(at.session_state['deleted_body']['claim_ids'],[])

    def test_manual_page_selection_survives_pagination(self):
        at=self.app()
        self.button(at,'选中当前页全部').click().run()
        at.session_state['fixture_page']=2
        at.run()
        self.button(at,'选中当前页全部').click().run()
        self.button(at,'删除选中的 4 条 claim').click().run()
        self.assertFalse(at.exception,[e.message for e in at.exception])
        self.assertEqual(at.session_state['deleted_body']['claim_ids'],[1,2,3,4])
        self.assertFalse(at.session_state['deleted_body']['all_matching'])

    def test_document_all_selection_submits_one_visible_maintenance_job(self):
        at=self.app(documents=True)
        at.checkbox[0].check().run()
        self.button(at,'重新处理全部文献').click().run()
        self.assertFalse(at.exception,[e.message for e in at.exception])
        self.assertEqual(at.session_state['reprocess_body'],{'mode':'reparse','document_id':None})
        self.assertIn('文献处理任务',[item.value for item in at.subheader])
