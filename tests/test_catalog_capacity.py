"""Capacity regressions: controlled provider, cached subdivisions, no live model."""
from contextlib import contextmanager
from collections import Counter
import copy
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.catalog import source_contains, split_catalog_segment
from app.schemas import CatalogBundle, CatalogEntity


class SegmentCache:
    def __init__(self):
        self.rows, self.attempts = {}, []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, query, params):
        if query.startswith('SELECT payload'):
            key = ('catalog_split',params[1]) if "stage='catalog_split'" in query else (params[1],params[2])
            self.row = {'payload':copy.deepcopy(self.rows[key])} if key in self.rows else None
        elif query.startswith('INSERT INTO processing_cache'):
            stage, key, payload = ('catalog_split',params[1],params[2]) if "'catalog_split'" in query else (params[1],params[2],params[3])
            self.rows[(stage,key)] = copy.deepcopy(payload.obj) if 'DO UPDATE SET payload' in query else self.rows.get((stage,key),copy.deepcopy(payload.obj))
        elif query.startswith('INSERT INTO processing_attempts'):
            self.attempts.append({'status':'failed' if "'failed'" in query else 'done','output':copy.deepcopy(params[4].obj)})
        elif query.startswith('DELETE FROM processing_cache'):
            self.rows.pop((params[1],params[2]),None)
        else:
            raise AssertionError(query)
        return self

    def fetchone(self):
        return self.row


class CatalogCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ,TEST_ENV):
            from app import llm,tasks
        cls.llm,cls.tasks=llm,tasks

    def setUp(self):
        for name, value in [('CATALOG_MAX_OUTPUT_TOKENS', 4096),
                            ('CATALOG_OUTPUT_RETRY_MAX_TOKENS', 16384)]:
            setting = patch.object(self.tasks, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def test_split_preserves_all_characters_absolute_offsets_and_overlap(self):
        for text in ['x'*2001, 'Alpha protein. '*180, '细胞类型及表型。'*300, 'ab']:
            segment={'id':1,'section_title':'Results','text':text,'start':321,'end':321+len(text)}
            children=split_catalog_segment(segment,min_chars=1)
            restored=['']*len(text)
            for child in children:
                self.assertLess(len(child['text']),len(text))
                self.assertEqual(child['text'],text[child['start']-321:child['end']-321])
                restored[child['start']-321:child['end']-321]=child['text']
            self.assertEqual(''.join(restored),text)
        self.assertEqual(split_catalog_segment({'text':'tiny'},min_chars=4),[])
        with self.assertRaises(ValueError):
            split_catalog_segment({'text':'tiny'},min_chars=0)

    def test_length_finish_is_not_accepted_even_for_parseable_json_or_retried(self):
        raw='{"entities":[]}'
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw),finish_reason='length')])
        with patch.object(self.llm.client.chat.completions,'create',return_value=response) as request:
            with self.assertRaises(self.llm.ModelOutputTruncatedError) as caught:
                self.llm.chat_completion([],max_tokens=1234)
        self.assertEqual(caught.exception.raw,raw)
        self.assertIn('1234',str(caught.exception))
        request.assert_called_once()

    def test_truncation_diagnostics_count_reasoning_without_exposing_it(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='', reasoning_content='private reasoning'),
                                     finish_reason='length')],
            usage=SimpleNamespace(completion_tokens=4096,
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=4096)))
        with patch.object(self.llm.client.chat.completions, 'create', return_value=response):
            with self.assertRaises(self.llm.ModelOutputTruncatedError) as caught:
                self.llm.chat_completion([], max_tokens=4096)
        self.assertEqual(caught.exception.diagnostics,
            {'content_chars':0, 'reasoning_chars':17, 'completion_tokens':4096, 'reasoning_tokens':4096})
        self.assertNotIn('private reasoning', str(caught.exception))
        self.assertIn('reasoning_tokens=4096', str(caught.exception))

    def test_catalog_output_budget_is_independent_of_claims(self):
        with patch.object(self.llm,'CATALOG_MAX_OUTPUT_TOKENS',1234), patch.object(self.llm,'chat_completion',return_value='{"entities":[]}') as chat:
            self.llm.extract_catalog('Results','Source text.')
            self.assertEqual(chat.call_args.kwargs['max_tokens'],1234)
            self.llm.extract_catalog('Results','Source text.', max_tokens=8192)
            self.assertEqual(chat.call_args.kwargs['max_tokens'],8192)
        with patch.object(self.llm,'chat_completion',return_value='{"claims":[]}',) as chat:
            self.llm.extract_claims('Results','Source text.')
            self.assertEqual(chat.call_args.kwargs,{'max_tokens':self.llm.CLAIMS_MAX_OUTPUT_TOKENS})

    def test_truncation_subdivides_once_and_completed_tree_is_reused(self):
        cache=SegmentCache()
        source='Alpha protein was measured. Beta protein was measured. Gamma protein was measured. Delta protein was measured.'
        segment={'id':7,'section_title':'Results','text':source,'start':100,'end':100+len(source)}
        calls=[]
        def model(heading,text,**kwargs):
            calls.append(text)
            self.assertEqual(kwargs,{})  # No correction of the same oversized input.
            if len(text)>80:
                raise self.llm.ModelOutputTruncatedError('capacity exceeded','{"entities":[')
            return CatalogBundle(entities=[CatalogEntity(text=name,type='Protein',evidence=name)
                for name in ['Alpha protein','Beta protein','Gamma protein','Delta protein'] if source_contains(text,name)])
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect),patch.object(self.tasks,'extract_catalog',side_effect=model):
            leaves=list(self.tasks.catalog_segment_bundles(1,'Study',segment,0,min_chars=20))
            first_calls=list(calls)
            repeated=list(self.tasks.catalog_segment_bundles(1,'Study',segment,0,min_chars=20))
        self.assertEqual(calls,first_calls)
        self.assertEqual(Counter(calls)[source],1)
        self.assertEqual({entity.text for _,b in leaves for entity in b.entities},{'Alpha protein','Beta protein','Gamma protein','Delta protein'})
        self.assertEqual(len(leaves),len(repeated))
        self.assertEqual([a['status'] for a in cache.attempts].count('failed'),1)
        self.assertTrue(any(stage=='catalog_split' for stage,_ in cache.rows))
        self.assertFalse(any(a['output']['raw']=='{"entities":[' for a in cache.attempts if a['status']=='done'))

    def test_child_failure_resumes_saved_success_without_repeating_parent(self):
        cache=SegmentCache()
        source='Alpha protein was measured. Beta protein was measured. Gamma protein was measured. Delta protein was measured.'
        segment={'id':7,'section_title':'Results','text':source,'start':0,'end':len(source)}
        calls=Counter()
        fail=[True]
        def model(heading,text):
            calls[text]+=1
            if len(text)>80:
                raise self.llm.ModelOutputTruncatedError('capacity exceeded','partial')
            if 'Delta' in text and fail[0]:
                raise ValueError('controlled child failure')
            return CatalogBundle(entities=[])
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect),patch.object(self.tasks,'extract_catalog',side_effect=model):
            with self.assertRaisesRegex(ValueError,'child failure'):
                list(self.tasks.catalog_segment_bundles(1,'Study',segment,0,min_chars=20))
            saved=[text for text in calls if text!=source and 'Delta' not in text]
            self.assertTrue(saved)
            fail[0]=False
            leaves=list(self.tasks.catalog_segment_bundles(1,'Study',segment,0,min_chars=20))
        self.assertTrue(leaves)
        self.assertEqual(calls[source],1)
        self.assertTrue(all(calls[text]==1 for text in saved))

    def test_minimum_input_still_truncated_fails_without_empty_success(self):
        cache=SegmentCache()
        segment={'id':7,'section_title':'Results','text':'A short source.','start':0,'end':15}
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect),patch.object(self.tasks,'extract_catalog',side_effect=self.llm.ModelOutputTruncatedError('capacity exceeded','partial')) as model:
            with self.assertRaisesRegex(self.llm.ModelOutputTruncatedError,'model server limits'):
                list(self.tasks.catalog_segment_bundles(1,'Study',segment,0,min_chars=500))
        self.assertEqual([call.kwargs.get('max_tokens', 4096) for call in model.call_args_list],
                         [4096, 8192, 16384])
        self.assertEqual(cache.rows,{})
        self.assertEqual([a['status'] for a in cache.attempts],['failed'] * 3)

    def test_minimum_segment_grows_budget_and_reuses_validated_success(self):
        cache = SegmentCache()
        source = 'Alpha protein was measured.'
        segment = {'id':7, 'section_title':'Results', 'text':source, 'start':0, 'end':len(source)}
        calls = []
        def model(heading, text, **kwargs):
            calls.append(kwargs)
            if not kwargs:
                raise self.llm.ModelOutputTruncatedError('capacity exceeded', 'partial')
            return CatalogBundle(entities=[CatalogEntity(text='Alpha protein', type='Protein',
                mention='Alpha protein', aliases=['Invented alias'], evidence=source)])
        with patch.object(self.tasks, 'get_conn', side_effect=cache.connect), \
             patch.object(self.tasks, 'extract_catalog', side_effect=model):
            leaves = list(self.tasks.catalog_segment_bundles(1, 'Study', segment, 0))
            with patch.object(self.tasks, 'CATALOG_OUTPUT_RETRY_MAX_TOKENS', 4096):
                repeated = list(self.tasks.catalog_segment_bundles(1, 'Study', segment, 0))
        self.assertEqual(calls, [{}, {'max_tokens':8192}])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0][1].model_dump(), repeated[0][1].model_dump())
        self.assertEqual(leaves[0][1].entities[0].aliases, [])  # Grounding still runs.
        self.assertTrue(leaves[0][1]._catalog_repairs)
        self.assertFalse(any(stage == 'catalog_split' for stage, _ in cache.rows))
        self.assertEqual([a['status'] for a in cache.attempts], ['failed', 'done'])

    def test_output_retry_ceiling_is_exact_and_can_disable_escalation(self):
        segment = {'id':7, 'section_title':'Results', 'text':'Short.', 'start':0, 'end':6}
        for ceiling, expected in [(12288, [4096,8192,12288]), (4096, [4096])]:
            cache = SegmentCache()
            with self.subTest(ceiling=ceiling), \
                 patch.object(self.tasks, 'CATALOG_OUTPUT_RETRY_MAX_TOKENS', ceiling), \
                 patch.object(self.tasks, 'get_conn', side_effect=cache.connect), \
                 patch.object(self.tasks, 'extract_catalog', side_effect=
                     self.llm.ModelOutputTruncatedError('capacity exceeded', 'partial',
                         diagnostics={'content_chars':7})) as model:
                with self.assertRaises(self.llm.ModelOutputTruncatedError) as caught:
                    list(self.tasks.catalog_segment_bundles(1, 'Study', segment, 0))
                self.assertEqual([call.kwargs.get('max_tokens',4096) for call in model.call_args_list], expected)
                self.assertIn(f'attempted max_tokens={expected}', str(caught.exception))
                self.assertIn('content_chars=7', str(caught.exception))
                self.assertEqual(caught.exception.raw, 'partial')
                self.assertEqual(cache.rows, {})

    def test_capacity_escalation_does_not_retry_server_or_other_errors(self):
        cache = SegmentCache()
        segment = {'id':7, 'section_title':'Results', 'text':'Short.', 'start':0, 'end':6}
        with patch.object(self.tasks, 'get_conn', side_effect=cache.connect), \
             patch.object(self.tasks, 'extract_catalog', side_effect=[
                 self.llm.ModelOutputTruncatedError('capacity exceeded', 'partial'),
                 RuntimeError('server rejected output budget')]) as model:
            with self.assertRaisesRegex(RuntimeError, 'server rejected output budget'):
                list(self.tasks.catalog_segment_bundles(1, 'Study', segment, 0))
        self.assertEqual(model.call_count, 2)
        self.assertEqual(cache.rows, {})

    def test_invalid_output_budgets_fail_before_model_or_database(self):
        segment = {'id':7, 'section_title':'Results', 'text':'Short.', 'start':0, 'end':6}
        for base, ceiling in [(0,16384), (4096,2000)]:
            with self.subTest(base=base, ceiling=ceiling), \
                 patch.object(self.tasks, 'CATALOG_MAX_OUTPUT_TOKENS', base), \
                 patch.object(self.tasks, 'CATALOG_OUTPUT_RETRY_MAX_TOKENS', ceiling), \
                 patch.object(self.tasks, 'get_conn') as db, \
                 patch.object(self.tasks, 'extract_catalog') as model:
                with self.assertRaisesRegex(ValueError, 'Catalog output budgets'):
                    list(self.tasks.catalog_segment_bundles(1, 'Study', segment, 0))
                db.assert_not_called()
                model.assert_not_called()


if __name__=='__main__':
    unittest.main()
