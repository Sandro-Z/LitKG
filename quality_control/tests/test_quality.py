import io
import copy
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from quality_control.models import normalize, anchor, blocks, SCENARIOS
from quality_control.engine import evaluate
from quality_control.ontology import validate
from quality_control.store import Store
from quality_control.server import Service, handler
from quality_control.scoring import scenario_scores
from quality_control.prompts import EXTRACT, FIDELITY, MATCH, RUBRIC
from quality_control.llm import LLMError, Client


def fixture():
    return {'name': 'Fixture', 'claims': [
        {'id': 'c1', 'subject': {'id': 'a', 'text': 'Alpha', 'type': 'Drug'}, 'predicate': 'TREATS',
         'object': {'id': 'b', 'text': 'Beta', 'type': 'Disease'}, 'document_id': 'd1',
         'source_refs': [{'doc_id': 'd1', 'block_id': 'b1', 'quote': 'Alpha treats Beta.'}]}],
        'documents': [{'id': 'd1', 'parsing_status': 'complete', 'blocks': [
            {'id': 'b1', 'text': 'Alpha treats Beta.', 'page': 2}]}],
        'policy': {'scope': 'Drug treatment outcomes', 'ontology': {
            'node_types': {'Drug': {}, 'Disease': {}},
            'predicates': {'TREATS': {'domain': ['Drug'], 'range': ['Disease']}}}}}


class FakeLLM:
    enabled = True
    model = 'deterministic-fixture'
    calls = 0
    usage = {}
    def __init__(self, bad_anchor=False, unsupported=False, match='matched'):
        self.seen = []
        self.bad_anchor, self.unsupported, self.match = bad_anchor, unsupported, match
    def ask(self, instruction, data):
        self.calls += 1
        self.seen.append((instruction, copy.deepcopy(data)))
        ref = {'doc_id': 'd1', 'block_id': 'b1', 'quote': 'Alpha treats Beta.', 'role': 'supports'}
        if instruction == EXTRACT:
            return {'candidates': [{'subject': 'Alpha', 'predicate': 'TREATS', 'object': 'Beta',
                                    'qualifiers': {}, 'evidence': [ref]}],
                    'noun_phrases': [{'text': 'Alpha', 'evidence': [ref]}, {'text': 'Beta', 'evidence': [ref]}],
                    'predicate_mentions': [{'text': 'treats', 'evidence': [ref]}], 'negations': []}
        if instruction == FIDELITY:
            if self.bad_anchor:
                ref['quote'] = 'Fabricated evidence.'
            return {'assessments': [{'claim_id': data['claims'][0]['id'],
                                     'verdict': 'UNSUPPORTED' if self.unsupported else 'SUPPORTED',
                                     'rationale': 'Direct source evidence', 'evidence': [] if self.unsupported else [ref],
                                     'specialization': 90, 'granularity': 95}]}
        if instruction == MATCH:
            return {'matches': [{'candidate_id': data['candidates'][0]['id'], 'status': self.match,
                                  'claim_ids': [data['claims'][0]['id']] if self.match in {'matched', 'misrepresented'} else [],
                                  'rationale': 'Equivalent source-grounded claim'}], 'identity_issues': []}
        if instruction == RUBRIC:
            if self.bad_anchor:
                ref['quote'] = 'Fabricated evidence.'
            return {'claim_id': data['claim']['id'], 'specialization': 90, 'granularity': 95, 'evidence': [ref]}
        raise AssertionError('Unexpected prompt')


class ImportTests(unittest.TestCase):
    def test_adapters_and_no_silent_data_loss(self):
        for payload in ([['Alpha', 'TREATS', 'Beta']], {'triples': [{'s': 'Alpha', 'p': 'TREATS', 'o': 'Beta'}]},
                        {'relations': [{'head': 'Alpha', 'relation': 'TREATS', 'tail': 'Beta'}]},
                        {'claims': [{'subject_text': 'Alpha', 'predicate': 'TREATS', 'object_text': 'Beta'}]}):
            graph = normalize(payload)
            self.assertEqual(1, len(graph['claims']))
            self.assertEqual(2, len(graph['nodes']))
            self.assertIn('original', graph['claims'][0])
            self.assertNotIn('negated', graph['claims'][0]['qualifiers'])
        graph = normalize({'claims': [{'x': {'h': 'Alpha', 'r': 'TREATS', 't': 'Beta'}}]},
                          mapping={'subject': 'x.h', 'predicate': 'x.r', 'object': 'x.t'})
        self.assertEqual('TREATS', graph['claims'][0]['predicate'])

    def test_canonical_literal_identity_and_qualifiers(self):
        p = {'graph': {'nodes': [{'id': 'n1', 'label': 'A', 'type': 'Drug'}, {'id': 'n2', 'label': 'A', 'type': 'Drug'}],
                       'claims': [{'id': 'x', 'subject': 'n1', 'predicate': 'value',
                                   'object': {'kind': 'literal', 'value': 4, 'datatype': 'integer'},
                                   'qualifiers': {'negated': True, 'context': {'unit': 'mg'}}}]}}
        g = normalize(p)
        self.assertEqual(2, len(g['nodes']))
        self.assertEqual(4, g['claims'][0]['object']['value'])
        self.assertTrue(g['claims'][0]['qualifiers']['negated'])

    def test_litkg_nested_claim_and_refs(self):
        raw = fixture()['claims'][0]
        p = {'claims': [{'id': 13, 'document_id': 6, 'claim_json': raw,
                         'source_refs': [{'document_id': 6, 'chunk_id': 7, 'sentence': 'X'}]}]}
        c = normalize(p)['claims'][0]
        self.assertEqual('13', c['id'])
        self.assertEqual('6', c['document_id'])
        self.assertEqual(7, c['source_refs'][0]['chunk_id'])

    def test_duplicate_ids_rejected_and_input_immutable(self):
        p = fixture()
        before = copy.deepcopy(p)
        normalize(p)
        self.assertEqual(before, p)
        p['claims'].append(copy.deepcopy(p['claims'][0]))
        with self.assertRaises(ValueError): normalize(p)

    def test_unicode_anchor_and_ambiguous_rejected(self):
        g = normalize({'claims': [], 'documents': [{'id': '文献', 'blocks': [{'id': '块', 'text': '甲😀乙。甲'}]}]})
        corpus = blocks(g)
        a = anchor({'doc_id': '文献', 'block_id': '块', 'quote': '😀乙'}, corpus)
        self.assertEqual((1, 3), (a['start'], a['end']))
        self.assertIsNone(anchor({'doc_id': '文献', 'block_id': '块', 'quote': '甲'}, corpus))
        self.assertIsNone(anchor({'doc_id': '文献', 'block_id': '块', 'quote': '伪造'}, corpus))

    def test_snapshot_fork_preserves_ids(self):
        old = normalize(fixture())
        new = normalize({'graph': {'nodes': old['nodes'], 'claims': old['claims']}, 'documents': old['documents'], 'policy': old['policy']})
        self.assertEqual([c['id'] for c in old['claims']], [c['id'] for c in new['claims']])
        self.assertEqual([n['id'] for n in old['nodes']], [n['id'] for n in new['nodes']])

    def test_endpoint_process_is_claim_specific_after_identity_reuse(self):
        from quality_control.models import public_claim
        p = fixture()
        p['claims'][0]['object']['process'] = {'kind': 'severity', 'direction': 'decreased'}
        other = copy.deepcopy(p['claims'][0]);other['id'] = 'c2'
        other['object']['process'] = {'kind': 'prevalence', 'direction': 'unchanged'}
        p['claims'].append(other)
        g = normalize(p)
        nodes = {n['id']: n for n in g['nodes']}
        self.assertEqual(2, len(nodes))
        self.assertEqual('severity', public_claim(g['claims'][0], nodes)['object']['process']['kind'])
        self.assertEqual('prevalence', public_claim(g['claims'][1], nodes)['object']['process']['kind'])


class ConstraintTests(unittest.TestCase):
    def test_good_graph_and_inheritance(self):
        p = fixture()
        p['policy']['ontology']['node_types']['Compound'] = {}
        p['policy']['ontology']['type_parents'] = {'Drug': 'Compound'}
        p['policy']['ontology']['predicates']['TREATS']['domain'] = ['Compound']
        self.assertEqual(100, validate(normalize(p))['score'])

    def test_every_constraint_family(self):
        g = normalize(fixture())
        a = g['nodes'][0]
        a.update(types=['Drug', 'Disease'], count='wrong', dose=100, dose_unit='kg', tags=['same', 'same'])
        ont = g['policy']['ontology']
        ont['disjoint'] = [['Drug', 'Disease']]
        ont['node_properties'] = {'required_value': {'required': True}, 'count': {'datatype': 'integer'},
                                  'dose': {'datatype': 'number', 'maximum': 10, 'units': ['mg']},
                                  'tags': {'unique': True, 'max_count': 1}}
        ont['predicates']['TREATS'].update(domain=['Protein'], min_count=2, max_count=0)
        rules = {f['rule'] for f in validate(g)['findings']}
        self.assertTrue({'disjoint', 'required', 'datatype', 'range', 'unit', 'unique', 'cardinality', 'domain_range'} <= rules)

    def test_no_ontology_and_zero_edges_not_perfect(self):
        g = normalize({'claims': []})
        r = evaluate(g)
        self.assertIsNone(r['dimensions']['ontology'])
        self.assertIsNone(r['metrics']['strict_support_rate'])
        self.assertEqual('INCOMPLETE', r['gate'])
        self.assertTrue(all(s['overall'] is None for s in r['scenarios'].values()))

    def test_dangling_is_blocker(self):
        g = normalize({'graph': {'nodes': [], 'claims': [{'subject': {'kind': 'entity', 'value': 'absent'},
            'predicate': 'p', 'object': {'kind': 'literal', 'value': 3}}]}})
        r = evaluate(g)
        self.assertEqual('FAIL', r['gate'])
        self.assertTrue(all(s['suitability'] == '不适用' for s in r['scenarios'].values()))


class WorkflowTests(unittest.TestCase):
    def test_extraction_is_independent_and_report_scores(self):
        graph, llm = normalize(fixture()), FakeLLM()
        r = evaluate(graph, llm)
        self.assertEqual('PASS', r['gate'])
        self.assertEqual(100, r['dimensions']['correctness'])
        self.assertEqual(100, r['dimensions']['completeness'])
        self.assertIsNotNone(r['scenarios']['field_review']['overall'])
        first = llm.seen[0][1]
        self.assertNotIn('claims', first)
        self.assertNotIn('nodes', first)
        self.assertEqual([EXTRACT, FIDELITY, MATCH], [x[0] for x in llm.seen])
        self.assertIn('candidate_hash', r)

    def test_fabricated_evidence_cannot_pass(self):
        r = evaluate(normalize(fixture()), FakeLLM(bad_anchor=True))
        self.assertEqual('INSUFFICIENT_EVIDENCE', r['assessments'][0]['verdict'])
        self.assertIsNone(r['dimensions']['correctness'])
        self.assertFalse(r['complete'])

    def test_partial_source_forbids_unsupported_and_final_score(self):
        p = fixture();p['documents'][0]['parsing_status'] = 'partial'
        r = evaluate(normalize(p), FakeLLM(unsupported=True))
        self.assertEqual('INSUFFICIENT_EVIDENCE', r['assessments'][0]['verdict'])
        self.assertIsNone(r['scenarios']['field_review']['overall'])

    def test_missing_scope_keeps_completeness_provisional(self):
        p = fixture();p['policy']['scope'] = None
        r = evaluate(normalize(p), FakeLLM())
        self.assertEqual(100, r['dimensions']['completeness'])
        self.assertEqual('provided_source_text_only', r['dimension_details']['completeness']['basis'])
        self.assertIsNone(r['scenarios']['field_review']['overall'])
        self.assertEqual('待验证', r['scenarios']['field_review']['suitability'])

    def test_context_budget_explicit_not_silent_truncation(self):
        r = evaluate(normalize(fixture()), FakeLLM(), max_chars=2)
        self.assertTrue(any(e['code'] == 'CONTEXT_LIMIT' for e in r['errors']))
        self.assertIsNone(r['dimensions']['correctness'])

    def test_provider_failure_never_perfect(self):
        llm = FakeLLM()
        def fail(*args): raise LLMError('LLM_HTTP_503')
        llm.ask = fail
        r = evaluate(normalize(fixture()), llm)
        self.assertFalse(r['complete'])
        self.assertIsNone(r['scenarios']['field_review']['overall'])
        self.assertIn('LLM_HTTP_503', [e['code'] for e in r['errors']])

    def test_consecutive_bad_requests_stop_review(self):
        source = fixture()
        block = source['documents'][0]['blocks'][0]
        source['documents'][0]['blocks'] = [dict(block, id=f'b{i}') for i in range(6)]
        client = FakeLLM()
        calls = []

        def reject(prompt, data):
            calls.append(data)
            raise LLMError('LLM_HTTP_400')

        client.ask = reject
        report = evaluate(normalize(source), client)
        self.assertEqual(3, len(calls))
        self.assertEqual('LLM_REPEATED_HTTP_400', report['failure_code'])
        self.assertEqual([], report['assessments'])
        self.assertEqual(3, len(report['document_coverage']))
        self.assertFalse(report['complete'])

    def test_claim_request_failure_skips_rubric_and_verdict(self):
        client = FakeLLM()
        original = client.ask
        calls = []

        def reject_claim(prompt, data):
            calls.append(data)
            if 'claims' in data and 'candidates' not in data:
                raise LLMError('LLM_HTTP_400')
            return original(prompt, data)

        client.ask = reject_claim
        report = evaluate(normalize(fixture()), client)
        self.assertEqual([], report['assessments'])
        self.assertFalse(any('claim' in data for data in calls))
        failures = [error for error in report['errors'] if error['code'] == 'LLM_HTTP_400']
        self.assertEqual(1, len(failures))
        self.assertTrue(failures[0]['claim_id'])
        self.assertNotIn('failure_code', report)

    def test_misrepresentation_is_not_duplicate_omission(self):
        r = evaluate(normalize(fixture()), FakeLLM(match='misrepresented'))
        self.assertEqual({'misrepresented': 1}, r['metrics']['candidate_counts'])
        self.assertEqual(0, r['dimensions']['completeness'])
        self.assertEqual('NEEDS_REVISION', r['gate'])

    def test_long_source_keeps_anchor_and_forbids_unsupported(self):
        p = fixture()
        p['documents'][0]['blocks'].append({'id': 'long', 'text': 'Unrelated content. ' * 8000})
        llm = FakeLLM()
        r = evaluate(normalize(p), llm, max_chars=6000)
        requests = [d for prompt, d in llm.seen if prompt == FIDELITY]
        self.assertFalse(requests[0]['source_complete'])
        self.assertTrue(any(b['id'] == 'b1' for b in requests[0]['blocks']))
        self.assertEqual('SUPPORTED', r['assessments'][0]['verdict'])
        self.assertFalse(r['complete'])

    def test_large_graph_missing_requires_all_batches(self):
        p = fixture()
        p['claims'] = [dict(p['claims'][0], id=str(i)) for i in range(51)]
        llm = FakeLLM()
        original = llm.ask
        def ask(prompt, data):
            if prompt == MATCH:
                llm.seen.append((prompt, copy.deepcopy(data)))
                return {'matches': [{'candidate_id': data['candidates'][0]['id'], 'status': 'not_found_in_batch',
                                     'claim_ids': [], 'rationale': 'No corresponding claim in this batch'}], 'identity_issues': []}
            return original(prompt, data)
        llm.ask = ask
        r = evaluate(normalize(p), llm)
        self.assertEqual('missing', r['matches'][0]['status'])
        self.assertEqual('NEEDS_REVISION', r['gate'])
        self.assertEqual(2, sum(prompt == MATCH for prompt, _ in llm.seen))

    def test_repeated_source_fact_freezes_one_candidate(self):
        p = fixture()
        p['documents'][0]['blocks'].append(copy.deepcopy(p['documents'][0]['blocks'][0]))
        p['documents'][0]['blocks'][-1]['id'] = 'b2'
        llm = FakeLLM()
        original = llm.ask
        def ask(prompt, data):
            v = original(prompt, data)
            if prompt == EXTRACT:
                for field in ('candidates', 'noun_phrases', 'predicate_mentions'):
                    for item in v[field]:
                        item['evidence'][0]['block_id'] = data['blocks'][0]['id']
            return v
        llm.ask = ask
        r = evaluate(normalize(p), llm)
        self.assertEqual(1, len(r['candidates']))
        self.assertEqual(2, len(r['candidates'][0]['evidence']))

    def test_long_plain_text_uses_windows_without_losing_original_anchors(self):
        p = fixture()
        p['documents'][0]['blocks'][0]['text'] = 'Alpha treats Beta.' + ' Background.' * 2000
        llm = FakeLLM()
        original = llm.ask
        def ask(prompt, data):
            if prompt == EXTRACT and 'Alpha treats Beta.' not in data['blocks'][0]['text']:
                llm.seen.append((prompt, copy.deepcopy(data)))
                return {'candidates': [], 'noun_phrases': [], 'predicate_mentions': [], 'negations': []}
            return original(prompt, data)
        llm.ask = ask
        r = evaluate(normalize(p), llm, max_chars=6000)
        self.assertEqual('SUPPORTED', r['assessments'][0]['verdict'])
        self.assertEqual('matched', r['matches'][0]['status'])
        self.assertEqual(1, len(r['document_coverage']))
        self.assertTrue(r['document_coverage'][0]['reviewed'])
        self.assertGreater(len(r['document_coverage'][0]['ranges']), 1)
        self.assertEqual(0, r['candidates'][0]['evidence'][0]['start'])

    def test_weights_change_between_scenarios_and_missing_not_zero(self):
        d = dict(specialization=90, granularity=90, completeness=40, correctness=90, ontology=100, coverage=40)
        r = scenario_scores(d, True)
        self.assertGreater(r['specific_information']['overall'], r['retrieval']['overall'])
        self.assertEqual('不适用', r['retrieval']['suitability'])
        d['completeness'] = None
        r = scenario_scores(d, False)
        self.assertIsNone(r['field_review']['overall'])
        self.assertLess(r['field_review']['measured_weight'], 1)

    def test_three_timeouts_stop_extraction_before_remaining_blocks(self):
        source = fixture()
        block = source['documents'][0]['blocks'][0]
        source['documents'][0]['blocks'] = [dict(block, id=f'b{i}') for i in range(7)]
        client = FakeLLM()
        calls = []

        def timeout(prompt, data):
            calls.append(data)
            raise LLMError('LLM_TIMEOUT')

        client.ask = timeout
        report = evaluate(normalize(source), client)
        self.assertEqual(3, len(calls))
        self.assertEqual(3, report['progress']['requests'])
        self.assertEqual('LLM_REPEATED_TIMEOUT', report['failure_code'])
        self.assertEqual([], report['assessments'])
        self.assertFalse(report['complete'])

    def test_run_limit_preserves_progress_and_prevents_further_calls(self):
        source = fixture()
        block = source['documents'][0]['blocks'][0]
        source['documents'][0]['blocks'] = [dict(block, id=f'b{i}') for i in range(7)]
        client = FakeLLM()
        original = client.ask
        calls = []

        def record(prompt, data):
            calls.append(data)
            return original(prompt, data)

        client.ask = record
        report = evaluate(normalize(source), client,
                        stop_reason=lambda: 'RUN_TIME_LIMIT' if calls else None)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, report['progress']['requests'])
        self.assertEqual('RUN_TIME_LIMIT', report['failure_code'])
        self.assertFalse(report['complete'])



class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'quality.sqlite3')
        p = fixture()
        p['claims'] = [dict(p['claims'][0], id=str(i)) for i in range(30)]
        self.graph = normalize(p)
        self.gid = self.store.add_graph(self.graph, p)
    def tearDown(self): self.tmp.cleanup()

    def test_uniform_sample_cap_fixed_and_reproducible(self):
        a = self.store.sample(self.gid)
        b = self.store.sample(self.gid)
        self.assertEqual(a['sample'], b['sample'])
        self.assertEqual(15, len(a['sample']['claim_ids']))
        self.assertEqual(15, len(set(a['sample']['claim_ids'])))
        import random
        expected = random.Random(int(a['sample']['seed'])).sample([c['id'] for c in self.graph['claims']], 15)
        self.assertEqual(expected, a['sample']['claim_ids'])

    def test_confident_graph_samples_ten(self):
        rid = self.store.start_run(self.gid, 'llm')
        self.store.update_run(rid, 'completed', {'uncertainty': 0})
        self.assertEqual(10, len(self.store.sample(self.gid)['sample']['claim_ids']))

    def test_history_and_sample_membership(self):
        sample = self.store.sample(self.gid)['sample']['claim_ids']
        cid = sample[0]
        self.store.review(self.gid, cid, 'r1', 'supported', 'source')
        state = self.store.review(self.gid, cid, 'r2', 'incorrect', 'correction')
        self.assertEqual(2, len(state['reviews']))
        self.assertEqual(2, len(state['history']))
        self.assertEqual(50, state['consensus'][0]['score'])
        self.assertTrue(state['consensus'][0]['disagreement'])
        with self.assertRaises(ValueError): self.store.review(self.gid, 'not-sampled', 'r1', 'supported', '')

    def test_restart_retains_partial_and_marks_interrupted(self):
        rid = self.store.start_run(self.gid, 'llm')
        self.store.update_run(rid, 'running', {'assessments': []})
        self.store.recover()
        run = self.store.run(rid)
        self.assertEqual('interrupted', run['status'])
        self.assertIsNotNone(run['report'])

    def test_no_duplicate_running_job(self):
        self.store.start_run(self.gid, 'llm')
        with self.assertRaises(ValueError): self.store.start_run(self.gid, 'llm')

    def test_delete_graph_removes_all_results_and_preserves_other_graphs(self):
        import sqlite3

        target = self.gid
        other = self.store.add_graph(self.graph, fixture())

        def add_results(graph_id):
            run_ids = []
            for status in ('completed', 'failed'):
                run_id = self.store.start_run(graph_id, 'llm')
                self.store.update_run(run_id, status, {'assessments': []})
                run_ids.append(run_id)
            for index in range(2):
                state = self.store.sample(graph_id, True, bool(index), 'fixture round')
                sample = state['sample']
                self.store.review(graph_id, sample['claim_ids'][0], 'fixture reviewer',
                                  'supported', 'fixture rationale', sample['id'])
            return run_ids

        removed_runs = add_results(target)
        retained_runs = add_results(other)

        def result_counts(graph_id):
            with self.store.connect() as db:
                return [db.execute(f'SELECT COUNT(*) FROM {table} WHERE graph_id=?', (graph_id,)).fetchone()[0]
                        for table in ('runs', 'samples', 'review_rounds', 'reviews')]

        self.assertEqual([2, 1, 2, 2], result_counts(target))
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER reject_delete BEFORE DELETE ON graphs
                        BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_graph(target)
        self.assertEqual([2, 1, 2, 2], result_counts(target))
        with self.store.connect() as db:
            db.execute('DROP TRIGGER reject_delete')

        self.store.delete_graph(target)
        self.assertEqual([0, 0, 0, 0], result_counts(target))
        with self.assertRaises(KeyError):
            self.store.graph(target)
        for run_id in removed_runs:
            with self.assertRaises(KeyError):
                self.store.run(run_id)
        self.assertEqual([2, 1, 2, 2], result_counts(other))
        self.assertEqual(other, self.store.run(retained_runs[0])['graph_id'])



class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Store(Path(self.tmp.name) / 'db.sqlite3'), FakeLLM)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.service.pool.shutdown(wait=True);self.tmp.cleanup()
    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers or ({'Content-Type': 'application/json'} if body is not None else {}))
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_import_run_poll_sample_source_review_export(self):
        gid = self.request('/api/graphs', {'payload': fixture()})['id']
        run = self.request(f'/api/graphs/{gid}/runs', {'mode': 'llm'})
        for _ in range(100):
            run = self.request('/api/runs/' + run['id'])
            if run['status'] not in {'running', 'queued'}: break
            time.sleep(.01)
        self.assertEqual('completed', run['status'])
        self.assertEqual('PASS', run['report']['gate'])
        self.request(f'/api/graphs/{gid}/sample', {})
        evidence = self.request(f'/api/graphs/{gid}/claims/c1')
        self.assertTrue(evidence['evidence'][0]['verified'])
        self.request(f'/api/graphs/{gid}/reviews', {'claim_id': 'c1', 'reviewer': 'test', 'verdict': 'supported'})
        export = self.request(f'/api/graphs/{gid}/export')
        self.assertEqual(1, export['human']['metrics']['reviewed'])
        self.assertEqual(1, len(self.request('/api/graphs')['graphs']))

    def test_static_and_origin_and_path_guards(self):
        with urllib.request.urlopen(self.base + '/') as r:
            self.assertIn('图谱质控'.encode(), r.read())
            self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/graphs', {'payload': fixture()}, {'Content-Type': 'application/json', 'Origin': 'https://example.invalid'})
        self.assertEqual(403, error.exception.code)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request('/../server.py')
        missing.exception.close()

    def test_local_import_only_configured_endpoint(self):
        with self.assertRaises(ValueError): self.service.local_get('/documents/1')
        with patch.object(self.service, 'local_get', return_value=fixture()) as get:
            self.request('/api/import/litkg', {'knowledge_base_id': 2})
            get.assert_called_once_with('/knowledge-bases/2/quality-export', max_bytes=128 * 1024 * 1024)

    def test_graph_delete_requires_confirmation_and_rejects_active_runs(self):
        graph_id = self.request('/api/graphs', {'payload': fixture()})['id']
        path = f'/api/graphs/{graph_id}/delete'
        body = {'confirm_graph_id': graph_id}
        with self.assertRaises(urllib.error.HTTPError) as wrong:
            self.request(path, {'confirm_graph_id': 'different'})
        with wrong.exception as response:
            self.assertEqual(400, response.code)
            self.assertEqual('GRAPH_DELETE_CONFIRMATION_REQUIRED', json.load(response)['error'])
        with self.assertRaises(urllib.error.HTTPError) as origin:
            self.request(path, body, {'Content-Type': 'application/json', 'Origin': 'https://example.invalid'})
        with origin.exception as response:
            self.assertEqual(403, response.code)

        run_id = self.service.store.start_run(graph_id, 'llm')
        for status in ('queued', 'running'):
            self.service.store.update_run(run_id, status)
            with self.assertRaises(urllib.error.HTTPError) as busy:
                self.request(path, body)
            with busy.exception as response:
                self.assertEqual(409, response.code)
                self.assertEqual('GRAPH_REVIEW_ACTIVE', json.load(response)['error'])
            self.assertEqual(status, self.service.store.run(run_id)['status'])

        self.service.store.update_run(run_id, 'failed', {'errors': []})
        self.assertEqual({'deleted': graph_id}, self.request(path, body))
        self.assertEqual([], self.request('/api/graphs')['graphs'])
        for missing_path in (f'/api/graphs/{graph_id}/export', f'/api/runs/{run_id}'):
            with self.assertRaises(urllib.error.HTTPError) as missing:
                self.request(missing_path)
            with missing.exception as response:
                self.assertEqual(404, response.code)
        with self.assertRaises(urllib.error.HTTPError) as repeat:
            self.request(path, body)
        with repeat.exception as response:
            self.assertEqual(404, response.code)

    def test_cancel_review_preserves_report_and_is_scoped_to_one_run(self):
        first = self.request('/api/graphs', {'payload': fixture()})['id']
        second = self.request('/api/graphs', {'payload': fixture()})['id']
        store = self.service.store
        first_run = store.start_run(first, 'llm')
        second_run = store.start_run(second, 'llm')
        store.update_run(first_run, 'running', {'progress': {'requests': 2}})
        self.assertEqual({'cancel_requested': True}, self.request(f'/api/runs/{first_run}/cancel', {}))
        self.assertTrue(store.run_cancel_requested(first_run))
        self.assertFalse(store.run_cancel_requested(second_run))
        store.update_run(first_run, 'running', {'progress': {'requests': 3}})
        self.assertTrue(store.run_cancel_requested(first_run))
        store.update_run(first_run, 'completed', {'progress': {'requests': 3}, 'complete': True})
        result = self.request(f'/api/runs/{first_run}')
        self.assertEqual('cancelled', result['status'])
        self.assertEqual('RUN_CANCELLED', result['error'])
        self.assertEqual(3, result['report']['progress']['requests'])
        self.assertFalse(result['report']['complete'])
        self.assertEqual('queued', self.request(f'/api/runs/{second_run}')['status'])
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.request(f'/api/runs/{first_run}/cancel', {})
        with conflict.exception as response:
            self.assertEqual(409, response.code)

    def test_cancel_stops_worker_after_current_model_request(self):
        source = fixture()
        block = source['documents'][0]['blocks'][0]
        source['documents'][0]['blocks'] = [dict(block, id=f'b{i}') for i in range(4)]
        graph_id = self.request('/api/graphs', {'payload': source})['id']
        client = FakeLLM()
        original = client.ask
        started = threading.Event()
        release = threading.Event()
        calls = []

        def block_first_request(prompt, data):
            calls.append(data)
            started.set()
            if not release.wait(5):
                raise AssertionError('fixture request was not released')
            return original(prompt, data)

        client.ask = block_first_request
        self.service.client_factory = lambda: client
        run_id = self.service.submit(graph_id, 'llm')['id']
        try:
            self.assertTrue(started.wait(3))
            self.assertEqual({'cancel_requested': True},
                             self.request(f'/api/runs/{run_id}/cancel', {}))
        finally:
            release.set()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = self.request(f'/api/runs/{run_id}')
            if run['status'] not in ('queued', 'running'):
                break
            time.sleep(0.01)
        self.assertEqual('cancelled', run['status'])
        self.assertEqual('RUN_CANCELLED', run['error'])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, run['report']['progress']['requests'])
        self.assertFalse(run['report']['complete'])

    def test_graph_overview_and_report_pages_keep_bulk_data_off_polling(self):
        source = fixture()
        claim = source['claims'][0]
        source['claims'] = [dict(claim, id=str(i)) for i in range(110)]
        graph_id = self.request('/api/graphs', {'payload': source})['id']
        store = self.service.store
        run_id = store.start_run(graph_id, 'llm')
        assessments = [{'claim_id': str(i), 'verdict': 'SUPPORTED'} for i in range(110)]
        store.update_run(run_id, 'completed', {'assessments': assessments, 'dimensions': {'ontology': 80},
                                               'errors': [{'code': 'TEST'}] * 110})
        with patch.object(store, 'graph', side_effect=AssertionError('overview must not deserialize the graph')):
            graph = self.request('/api/graphs?view=overview')['graphs'][0]
            self.assertEqual(110, graph['claim_count'])
            self.assertEqual(run_id, graph['latest_run']['id'])
            self.assertEqual({'ontology': 80}, graph['latest_run']['report']['dimensions'])
            self.assertNotIn('assessments', graph['latest_run']['report'])
            page = self.request(f'/api/graphs/{graph_id}/report?offset=50')['run']['report']
            self.assertEqual(110, page['preview']['assessments'])
            self.assertEqual(50, len(page['assessments']))
            self.assertEqual('50', page['assessments'][0]['claim_id'])
            self.assertEqual(80, len(page['errors']))
            self.assertEqual(110, page['preview']['errors'])
            self.assertIn('history', self.request(f'/api/graphs/{graph_id}/sample'))
            detail = self.request(f'/api/graphs/{graph_id}/claims/0')
            self.assertIn('claim', detail)
            self.assertIn('evidence', detail)
        self.assertEqual(110, len(self.request(f'/api/runs/{run_id}')['report']['assessments']))
        self.request(f'/api/graphs/{graph_id}/delete', {'confirm_graph_id': graph_id})
        self.assertEqual([], self.request('/api/graphs?view=overview')['graphs'])

    def test_delete_storage_errors_have_specific_responses(self):
        import sqlite3
        graph_id = self.request('/api/graphs', {'payload': fixture()})['id']
        failure = sqlite3.OperationalError('fixture database busy')
        failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch.object(self.service.store, 'delete_graph', side_effect=failure):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(f'/api/graphs/{graph_id}/delete', {'confirm_graph_id': graph_id})
            with error.exception as response:
                self.assertEqual(503, response.code)
                self.assertEqual('GRAPH_DELETE_BUSY', json.load(response)['error'])
        self.assertEqual(graph_id, self.request('/api/graphs?view=overview')['graphs'][0]['id'])

    def test_delete_reports_storage_exhaustion_separately(self):
        import sqlite3

        graph_id = self.request('/api/graphs', {'payload': fixture()})['id']
        failure = sqlite3.OperationalError('fixture storage exhausted')
        failure.sqlite_errorcode = sqlite3.SQLITE_FULL
        with patch.object(self.service.store, 'delete_graph', side_effect=failure):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(f'/api/graphs/{graph_id}/delete', {'confirm_graph_id': graph_id})
            with error.exception as response:
                self.assertEqual(507, response.code)
                self.assertEqual('GRAPH_DELETE_DISK_FULL', json.load(response)['error'])
        self.assertEqual(graph_id, self.request('/api/graphs?view=overview')['graphs'][0]['id'])

    def test_sqlite_temp_is_private_and_next_to_database(self):
        from importlib import import_module
        from stat import S_IMODE

        configure = import_module('quality_control.server').configure_sqlite_temp
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            os.environ.pop('SQLITE_TMPDIR', None)
            database = Path(directory) / 'data' / 'review.sqlite3'
            configure(database)
            expected = database.parent / '.sqlite-tmp'
            self.assertEqual(expected, Path(os.environ['SQLITE_TMPDIR']))
            self.assertEqual(0o700, S_IMODE(expected.stat().st_mode))
            os.environ['SQLITE_TMPDIR'] = directory
            configure(database)
            self.assertEqual(directory, os.environ['SQLITE_TMPDIR'])
            os.environ['SQLITE_TMPDIR'] = str(Path(directory) / 'missing')
            with self.assertRaisesRegex(RuntimeError, 'SQLITE_TEMP_DIRECTORY_UNWRITABLE'):
                configure(database)







class ExportTests(unittest.TestCase):
    def test_saved_chunks_including_unclaimed_blocks(self):
        # Test the adapter independently of optional LitKG runtime packages.
        import sys, types
        fake_export = types.ModuleType('app.claim_export')
        fake_export.collect_claims = lambda cur, **kw: {'claims': [], 'exported_count': 0}
        fake_types = types.ModuleType('app.entity_types');fake_types.TYPE_DEFINITIONS = {'Drug': 'drug'};fake_types.TYPE_PARENTS = {}
        fake_predicates = types.ModuleType('app.predicate_registry');fake_predicates.PREDICATES = {};fake_predicates.REGISTRY_VERSION = 'v1'
        spec = importlib.util.spec_from_file_location('qc_export_test', Path(__file__).parents[2] / 'app/app/quality_export.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'app.claim_export': fake_export, 'app.entity_types': fake_types, 'app.predicate_registry': fake_predicates}):
            spec.loader.exec_module(module)
        class Cursor:
            def execute(self, query, params): self.query = query
            def fetchall(self):
                if 'FROM documents' in self.query:
                    return [{'id': 1, 'title': 'paper', 'sha256': 'digest', 'status': 'done', 'pipeline_generation': 1}]
                return [{'id': 2, 'text': 'Source without any claims', 'page_start': 1, 'page_end': 1, 'page_spans': [], 'section_title': 'Intro'}]
        result = module.collect_quality_snapshot(Cursor(), 1)
        self.assertEqual('Source without any claims', result['documents'][0]['blocks'][0]['text'])
        self.assertEqual('partial', result['documents'][0]['parsing_status'])


class TransportTests(unittest.TestCase):
    def test_compatible_json_transport_and_error_body_is_private(self):
        from http.server import BaseHTTPRequestHandler
        seen = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, request))
                if len(seen) == 1:
                    body = json.dumps({'choices': [{'message': {'content': '{"result":"ok"}'}}],
                                       'usage': {'total_tokens': 12}}).encode()
                    self.send_response(200)
                else:
                    body = b'PRIVATE_PROVIDER_ERROR_BODY'
                    self.send_response(503)
                self.end_headers();self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            with patch.dict(os.environ, {'QC_LLM_BASE_URL': f'http://127.0.0.1:{server.server_port}/v1',
                                         'QC_LLM_MODEL': 'fixture-model', 'QC_LLM_API_KEY_FILE': ''}):
                client = Client()
            self.assertEqual({'result': 'ok'}, client.ask('fixed instruction', {'source': 'fixture'}))
            self.assertEqual('/v1/chat/completions', seen[0][0])
            self.assertEqual({'type': 'json_object'}, seen[0][1]['response_format'])
            self.assertEqual(12, client.usage['total_tokens'])
            with self.assertRaises(LLMError) as error:
                client.ask('fixed instruction', {'source': 'fixture'})
            self.assertEqual('LLM_HTTP_503', str(error.exception))
        finally:
            server.shutdown();server.server_close()

    def test_unsupported_response_format_uses_prompt_json(self):
        payloads = []

        class Response(io.BytesIO):
            pass

        class Opener:
            def open(self, request, timeout):
                payload = json.loads(request.data)
                payloads.append(payload)
                if 'response_format' in payload:
                    body = io.BytesIO(b'{"error":{"param":"response_format","message":"unsupported"}}')
                    raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {}, body)
                return Response(b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}')

        with patch.dict(os.environ, {'QC_LLM_BASE_URL': 'http://example.invalid/v1', 'QC_LLM_MODEL': 'fixture', 'QC_LLM_API_KEY_FILE': '', 'QC_LLM_API_KEY': ''}):
            client = Client()
            with patch.object(urllib.request, 'build_opener', return_value=Opener()):
                self.assertEqual({'ok': True}, client.ask('Return JSON.', {}))
                self.assertEqual({'ok': True}, client.ask('Return JSON.', {}))

        self.assertEqual([True, False, False], ['response_format' in p for p in payloads])
        self.assertTrue(all(p['temperature'] == 0 for p in payloads))

    def test_unreadable_key_does_not_disable_local_checks(self):
        with patch.dict(os.environ, {'QC_LLM_BASE_URL': 'http://localhost:1/v1', 'QC_LLM_MODEL': 'fixture',
                                     'QC_LLM_API_KEY_FILE': '/nonexistent-qc-fixture'}):
            client = Client()
        self.assertFalse(client.enabled)
        self.assertEqual('LLM_KEY_FILE_UNREADABLE', client.configuration_error)

    def test_model_timeout_is_distinct_from_malformed_response(self):
        client = Client()
        client.enabled = True
        self.assertEqual(300, client.request_timeout)
        with patch.object(client, '_request_json', side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaisesRegex(LLMError, '^LLM_TIMEOUT$'):
                client.ask('Return JSON.', {})
        with patch.object(client, '_request_json', side_effect=ValueError()):
            with self.assertRaises(LLMError) as invalid:
                client.ask('Return JSON.', {})
        self.assertNotEqual('LLM_TIMEOUT', str(invalid.exception))

    def test_streaming_json_preserves_usage_and_rejects_incomplete_stream(self):
        import importlib
        import io
        module = importlib.import_module(Client.__module__)
        events = [
            {"choices": [{"index": 0, "delta": {"content": "{\"ok\":"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "true}"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"completion_tokens": 6}},
        ]
        newline = chr(10)
        body = "".join("data: " + json.dumps(event) + newline * 2 for event in events)
        body += "data: [DONE]" + newline * 2
        result = module._read_review_stream(io.BytesIO(body.encode()))
        self.assertEqual({"ok": True}, json.loads(result["choices"][0]["message"]["content"]))
        self.assertEqual(6, result["usage"]["completion_tokens"])
        with self.assertRaisesRegex(LLMError, "LLM_INCOMPLETE_STREAM"):
            module._read_review_stream(io.BytesIO(("data: {}" + newline * 2).encode()))
        with self.assertRaisesRegex(LLMError, "LLM_CANCELLED"):
            module._read_review_stream(io.BytesIO(body.encode()), lambda: "cancelled")

    def test_evidence_ids_resolve_to_exact_source_and_reject_unknown_ids(self):
        import importlib
        module = importlib.import_module(Client.__module__)
        text = "First fact." + chr(10) + "Second fact. " + "x" * 1200
        data = {'blocks': [{"doc_id": "636", "id": "fixture", "text": text, 'window_start': 100}]}
        prepared, evidence = module._prepare_review_evidence(data)
        self.assertEqual(text, data['blocks'][0]["text"])
        self.assertEqual(text, "".join(item["quote"] for item in evidence.values()))
        for item in evidence.values():
            self.assertEqual(item["quote"], text[item["start"] - 100:item["end"] - 100])
        identifier = next(iter(evidence))
        value = module._resolve_review_evidence({"citations": [{"evidence_id": identifier, "kind": "support"}]}, evidence)
        self.assertEqual(evidence[identifier]["quote"], value["citations"][0]["quote"])
        self.assertEqual("support", value["citations"][0]["kind"])
        with self.assertRaisesRegex(LLMError, "LLM_INVALID_EVIDENCE_ID"):
            module._resolve_review_evidence({"evidence_id": "UNKNOWN"}, evidence)
        self.assertIn(identifier, prepared['blocks'][0]["text"])

    def test_qwen3_requests_explicit_output_budget_and_disable_thinking(self):
        from unittest.mock import patch
        client = Client()
        client.enabled = True
        client.model = "Qwen3-fixture"
        captured = []
        def response(payload, headers):
            captured.append(payload)
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        with patch.dict(os.environ, {'REVIEW_LLM_MAX_OUTPUT_TOKENS': "4096"}):
            with patch.object(client, '_request_json', side_effect=response):
                client.ask("Return a JSON object.", {})
        self.assertEqual(4096, captured[0]["max_tokens"])
        self.assertTrue(captured[0]["stream"])
        self.assertEqual({"include_usage": True}, captured[0]["stream_options"])
        self.assertEqual({"enable_thinking": False}, captured[0]["chat_template_kwargs"])



if __name__ == '__main__': unittest.main()
