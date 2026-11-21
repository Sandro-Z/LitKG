import copy
import itertools
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from quality_control.adaptive import (
    AdaptiveConcurrency, ReviewRequest, configured_max_concurrency, run_requests,
)
from quality_control.engine import evaluate
from quality_control.llm import Client, LLMError
from quality_control.models import normalize
from quality_control.prompts import EXTRACT, FIDELITY, MATCH, RUBRIC


class AdaptiveTests(unittest.TestCase):
    def test_configuration_has_hard_cap_and_safe_defaults(self):
        for value, expected in [('99', 4), ('0', 1), ('2', 2), ('invalid', 4)]:
            with patch.dict(os.environ, {'QC_LLM_MAX_CONCURRENCY': value}):
                self.assertEqual(configured_max_concurrency(), expected)

    def test_reduction_and_recovery_require_new_stable_successes(self):
        now = [0.0]
        c = AdaptiveConcurrency(clock=lambda: now[0])
        c.failure()
        self.assertEqual(c.limit, 2)
        for _ in range(20):
            c.success(0)
        self.assertEqual(c.limit, 2)
        for _ in range(8):
            c.success(c.epoch)
        self.assertEqual(c.limit, 2)
        now[0] = 20
        c.success(c.epoch)
        self.assertEqual(c.limit, 3)
        for _ in range(8):
            c.success(c.epoch)
        self.assertEqual(c.limit, 3)
        now[0] = 40
        c.success(c.epoch)
        self.assertEqual(c.limit, 4)
        c.failure()
        c.failure()
        c.failure()
        self.assertEqual(c.limit, 1)

    def test_real_requests_overlap_and_never_exceed_four(self):
        lock = threading.Lock()
        barrier = threading.Barrier(4, timeout=3)
        state = {'active': 0, 'peak': 0, 'calls': 0}
        results = []
        caller = threading.get_ident()

        class Provider:
            def ask(self, prompt, data):
                with lock:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                    state['calls'] += 1
                    call = state['calls']
                if call <= 4:
                    barrier.wait()
                time.sleep(0.005)
                with lock:
                    state['active'] -= 1
                return {'id': data['id']}

        def job(i):
            response = yield ReviewRequest('test', {'id': i})
            self.assertEqual(threading.get_ident(), caller)
            results.append(response['id'])

        c = AdaptiveConcurrency()
        run_requests((job(i) for i in range(24)), Provider(), c)
        self.assertEqual(state['peak'], 4)
        self.assertEqual(sorted(results), list(range(24)))
        self.assertEqual(c.snapshot()['requests'], 24)
        self.assertEqual(c.snapshot()['active'], 0)

    def test_transient_failure_retries_same_request_after_reduction(self):
        calls = []
        results = []
        c = AdaptiveConcurrency(clock=lambda: next(ticks))
        ticks = itertools.count()

        class Provider:
            def ask(self, prompt, data):
                calls.append(copy.deepcopy(data))
                if len(calls) == 1:
                    raise LLMError('LLM_HTTP_429')
                return {'ok': True}

        def job():
            results.append((yield ReviewRequest('test', {'id': 1})))

        run_requests([job()], Provider(), c)
        self.assertEqual(calls, [{'id': 1}, {'id': 1}])
        self.assertEqual(results, [{'ok': True}])
        self.assertEqual(c.limit, 2)
        self.assertEqual(c.retries, 1)

    def test_retry_budget_is_bounded(self):
        errors = []
        ticks = itertools.count()
        c = AdaptiveConcurrency(clock=lambda: next(ticks))

        class Provider:
            def ask(self, prompt, data):
                raise LLMError('LLM_TIMEOUT')

        def job():
            try:
                yield ReviewRequest('test', {})
            except LLMError as error:
                errors.append(str(error))

        run_requests([job()], Provider(), c)
        self.assertEqual(c.requests, 3)
        self.assertEqual(c.retries, 2)
        self.assertEqual(c.limit, 1)
        self.assertEqual(errors, ['LLM_TIMEOUT'])

    def test_live_reduction_drains_existing_calls_before_new_admission(self):
        lock = threading.Lock()
        first_wave = threading.Barrier(4, timeout=3)
        release = threading.Event()
        state = {'calls': 0, 'active': 0, 'later_peak': 0}
        results = []
        ticks = itertools.count()
        c = AdaptiveConcurrency(clock=lambda: next(ticks), recovery_seconds=100000)

        class Provider:
            def ask(self, prompt, data):
                with lock:
                    state['calls'] += 1
                    call = state['calls']
                    state['active'] += 1
                    if call > 4:
                        state['later_peak'] = max(state['later_peak'], state['active'])
                try:
                    if call <= 4:
                        first_wave.wait()
                        if call == 1:
                            raise LLMError('LLM_HTTP_503')
                        if not release.wait(3):
                            raise AssertionError('reduction was not observed')
                    time.sleep(0.005)
                    return {'id': data['id']}
                finally:
                    with lock:
                        state['active'] -= 1

        def job(i):
            results.append((yield ReviewRequest('test', {'id': i}))['id'])

        def changed():
            if c.limit == 2:
                release.set()

        run_requests((job(i) for i in range(20)), Provider(), c, changed=changed)
        self.assertEqual(sorted(results), list(range(20)))
        self.assertLessEqual(state['later_peak'], 2)
        self.assertEqual(c.limit, 2)
        self.assertEqual(c.retries, 1)

    def test_scheduler_probes_back_to_four_after_recovery(self):
        ticks = itertools.count()
        c = AdaptiveConcurrency(clock=lambda: next(ticks), recovery_seconds=0)
        calls = []
        results = []
        class Provider:
            def ask(self, prompt, data):
                calls.append(data['id'])
                if len(calls) == 1:
                    raise LLMError('LLM_HTTP_429')
                time.sleep(0.001)
                return {'id': data['id']}
        def job(i):
            results.append((yield ReviewRequest('test', {'id': i}))['id'])
        run_requests((job(i) for i in range(40)), Provider(), c)
        self.assertEqual(sorted(results), list(range(40)))
        self.assertEqual(c.limit, 4)
        self.assertEqual([e['current'] for e in c.events], [2, 3, 4])

    def test_permanent_rejection_and_truncation_reach_original_handler(self):
        for code in ['LLM_HTTP_400', 'LLM_HTTP_401', 'LLM_TRUNCATED_RESPONSE']:
            errors = []
            class Provider:
                def ask(self, prompt, data):
                    raise LLMError(code)
            def job():
                try:
                    yield ReviewRequest('test', {})
                except LLMError as error:
                    errors.append(str(error))
            c = AdaptiveConcurrency()
            run_requests([job()], Provider(), c)
            self.assertEqual(c.requests, 1)
            self.assertEqual(errors, [code])
            self.assertEqual(c.limit, 4)

    def test_cancellation_prevents_queued_calls(self):
        stopped = threading.Event()
        calls = []
        class Provider:
            def ask(self, prompt, data):
                calls.append(data['id'])
                stopped.set()
                return {}
        def job(i):
            yield ReviewRequest('test', {'id': i})
        c = AdaptiveConcurrency(maximum=1)
        run_requests((job(i) for i in range(20)), Provider(), c, stopped.is_set)
        self.assertEqual(calls, [0])
        self.assertEqual(c.active, 0)

    def test_shared_client_usage_is_accurate_under_parallel_calls(self):
        client = Client()
        client.enabled = True
        client.model = 'test'
        client.base = 'http://unused'
        client.key = ''
        client._request_json = lambda *_: {
            'usage': {'total_tokens': 3},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}],
        }
        def job():
            yield ReviewRequest('test', {})
        c = AdaptiveConcurrency()
        run_requests((job() for _ in range(100)), client, c)
        self.assertEqual(client.calls, 100)
        self.assertEqual(client.usage['total_tokens'], 300)


class EngineConcurrencyTests(unittest.TestCase):
    def graph(self):
        return normalize({
            'scope': 'All statements in these documents.',
            'claims': [{'id': 'c' + str(i), 'subject': 'A', 'predicate': 'binds',
                        'object': 'B', 'document_id': 'd' + str(i),
                        'source_refs': [{'doc_id': 'd' + str(i), 'block_id': 'b', 'quote': 'A binds B.'}]}
                       for i in range(8)],
            'documents': [{'id': 'd' + str(i), 'parsing_status': 'complete',
                           'blocks': [{'id': 'b', 'text': 'A binds B.'}]} for i in range(8)],
        })

    def provider(self):
        class Provider:
            enabled = True
            model = 'test'
            calls = 0
            usage = {}
            def __init__(self):
                self.lock = threading.Lock()
                self.active = self.peak = 0
                self.stages = {}
            def ask(self, prompt, data):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.stages[prompt] = max(self.stages.get(prompt, 0), self.active)
                    self.calls += 1
                time.sleep(0.015)
                block = data['blocks'][0]
                ref = {'doc_id': block['doc_id'], 'block_id': block['id'],
                       'quote': 'A binds B.', 'role': 'supports'}
                if prompt == EXTRACT:
                    response = {'candidates': [{'subject': 'A', 'predicate': 'binds', 'object': 'B',
                                               'qualifiers': {'document': block['doc_id']}, 'evidence': [ref]}]}
                elif prompt == FIDELITY:
                    response = {'assessments': [{'claim_id': data['claims'][0]['id'], 'verdict': 'SUPPORTED',
                                                'rationale': 'Exact evidence.', 'evidence': [ref]}]}
                elif prompt == RUBRIC:
                    response = {'claim_id': data['claim']['id'], 'specialization': 90,
                                'granularity': 95, 'evidence': [ref]}
                elif prompt == MATCH:
                    response = {'matches': [{'candidate_id': data['candidates'][0]['id'], 'status': 'matched',
                                             'claim_ids': [data['claims'][0]['id']], 'rationale': 'Exact match.'}]}
                else:
                    raise AssertionError('unexpected stage')
                with self.lock:
                    self.active -= 1
                return response
        return Provider()

    def test_extraction_fidelity_and_rubric_overlap_with_safe_checkpoints(self):
        provider = self.provider()
        thread = threading.get_ident()
        snapshots = []
        def checkpoint(report):
            self.assertEqual(threading.get_ident(), thread)
            snapshots.append(json.loads(json.dumps(report)))
        report = evaluate(self.graph(), provider, checkpoint)
        self.assertEqual(len(report['assessments']), 8)
        self.assertEqual(len(report['document_coverage']), 8)
        self.assertEqual(len(report['matches']), 8)
        self.assertTrue(all(a['verdict'] == 'SUPPORTED' for a in report['assessments']))
        self.assertEqual(report['errors'], [])
        self.assertEqual(provider.peak, 4)
        self.assertEqual(provider.stages[EXTRACT], 4)
        self.assertEqual(provider.stages[FIDELITY], 4)
        self.assertEqual(provider.stages[RUBRIC], 4)
        self.assertEqual(provider.stages[MATCH], 4)
        self.assertEqual(report['llm_concurrency']['peak_active'], 4)
        self.assertEqual(report['llm_concurrency']['active'], 0)
        self.assertTrue(snapshots)

    def test_parallel_and_serial_results_have_same_assessments_and_scores(self):
        with patch.dict(os.environ, {'QC_LLM_MAX_CONCURRENCY': '1'}):
            serial = evaluate(self.graph(), self.provider())
        with patch.dict(os.environ, {'QC_LLM_MAX_CONCURRENCY': '4'}):
            parallel = evaluate(self.graph(), self.provider())
        self.assertEqual(serial['dimensions'], parallel['dimensions'])
        self.assertEqual(serial['stages'], parallel['stages'])
        key = lambda item: item['claim_id']
        self.assertEqual(sorted(serial['assessments'], key=key), sorted(parallel['assessments'], key=key))


if __name__ == '__main__':
    unittest.main()
