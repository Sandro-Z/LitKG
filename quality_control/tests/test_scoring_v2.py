"""Regression coverage for missing dimension scores and multi-person review rounds."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_control.engine import evaluate
from quality_control.models import normalize
from quality_control.prompts import EXTRACT, FIDELITY, RUBRIC
from quality_control.rubrics import measure, RUBRICS
from quality_control.scoring import human_metrics
from quality_control.store import Store
import test_quality as fixtures
FakeLLM, fixture = fixtures.FakeLLM, fixtures.fixture


class DimensionTests(unittest.TestCase):
    def test_missing_scalars_trigger_independent_weighted_rubric(self):
        llm = FakeLLM()
        original = llm.ask
        def ask(prompt, data):
            response = original(prompt, data)
            if prompt == FIDELITY:
                response['assessments'][0].pop('specialization')
                response['assessments'][0].pop('granularity')
            if prompt == RUBRIC:
                response['specialization'] = {k: {'score': '80', 'rationale': 'Source identity'} for k in RUBRICS['specialization']}
                response['granularity'] = {k: {'score': '60%', 'rationale': 'Source qualification'} for k in RUBRICS['granularity']}
            return response
        llm.ask = ask
        result = evaluate(normalize(fixture()), llm)
        self.assertEqual(80, result['dimensions']['specialization'])
        self.assertEqual(60, result['dimensions']['granularity'])
        self.assertEqual('weighted_rubric', result['dimension_assessments'][0]['specialization']['method'])
        self.assertTrue(all(result['dimensions'][k] is not None for k in ('specialization', 'granularity', 'completeness', 'correctness', 'coverage')))
        self.assertEqual(4, len(result['dimension_assessments'][0]['specialization']['criteria']))

    def test_optional_extraction_fields_do_not_discard_valid_candidates(self):
        llm = FakeLLM();original = llm.ask
        def ask(prompt, data):
            response = original(prompt, data)
            if prompt == EXTRACT:
                return {'candidates': response['candidates']}
            return response
        llm.ask = ask
        p = fixture();p['policy']['scope'] = None;p['documents'][0]['parsing_status'] = 'partial'
        r = evaluate(normalize(p), llm)
        for field in ('specialization', 'granularity', 'completeness', 'correctness', 'coverage'):
            self.assertIsNotNone(r['dimensions'][field], field)
            self.assertEqual('partial', r['dimension_details'][field]['status'])
        self.assertEqual(2, len(r['noun_phrases']))
        self.assertEqual('provided_source_text_only', r['dimension_details']['completeness']['basis'])
        self.assertIsNotNone(r['scenarios']['field_review']['score'])
        self.assertIsNone(r['scenarios']['field_review']['overall'])

    def test_quote_whitespace_and_omitted_reference_ids_are_recovered(self):
        p = fixture();p['documents'][0]['blocks'][0]['text'] = 'Alpha\n treats Beta.'
        llm = FakeLLM();original = llm.ask
        def ask(prompt, data):
            response = original(prompt, data)
            if prompt == FIDELITY:
                response['assessments'][0]['evidence'] = [{'quote': 'Alpha treats Beta.'}]
            return response
        llm.ask = ask
        r = evaluate(normalize(p), llm)
        a = r['assessments'][0]
        self.assertEqual('SUPPORTED', a['verdict'])
        self.assertEqual('Alpha\n treats Beta.', a['evidence'][0]['quote'])
        self.assertTrue(a['evidence'][0]['recovered'])

    def test_score_strings_are_accepted_without_imputing_missing_values(self):
        llm = FakeLLM();original = llm.ask
        def ask(prompt, data):
            r = original(prompt, data)
            if prompt == FIDELITY:
                r['assessments'][0].update(specialization='85.5', granularity={'score': '75%'})
            return r
        llm.ask = ask
        r = evaluate(normalize(fixture()), llm)
        self.assertEqual(85.5, r['dimensions']['specialization'])
        self.assertEqual(75, r['dimensions']['granularity'])
        self.assertIsNone(measure(True, 'specialization', [{}]))
        self.assertIsNone(measure(95, 'specialization', []))

    def test_no_evidence_has_explicit_reason_instead_of_fabricated_score(self):
        llm = FakeLLM(bad_anchor=True)
        r = evaluate(normalize(fixture()), llm)
        self.assertIsNone(r['dimensions']['correctness'])
        self.assertIsNone(r['dimensions']['specialization'])
        self.assertEqual('unavailable', r['dimension_details']['correctness']['status'])
        self.assertTrue(r['dimension_details']['correctness']['reason'])
        self.assertEqual('completed_with_gaps', r['evaluation_status'])
        self.assertIn('INVALID_ASSESSMENT', r['failure_summary'])

    def test_partial_rubric_does_not_certify_full_report(self):
        llm = FakeLLM();original = llm.ask
        def ask(prompt, data):
            r = original(prompt, data)
            if prompt == FIDELITY:
                r['assessments'][0]['specialization'] = {'typing': {'score': 80}}
            return r
        llm.ask = ask
        r = evaluate(normalize(fixture()), llm)
        self.assertEqual(80, r['dimensions']['specialization'])
        self.assertEqual(.25, r['dimension_assessments'][0]['specialization']['coverage'])
        self.assertFalse(r['complete'])


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.store = Store(self.path)
        self.graph = normalize(fixture())
        self.gid = self.store.add_graph(self.graph, fixture())
    def tearDown(self): self.tmp.cleanup()
    def machine(self):
        rid = self.store.start_run(self.gid, 'llm')
        self.store.update_run(rid, 'completed', evaluate(self.graph, FakeLLM()))
        return rid

    def test_each_person_has_independent_vote_and_own_revision(self):
        self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'supported', 'first')
        self.store.review(self.gid, 'c1', 'B', 'incorrect', 'independent')
        state = self.store.review(self.gid, 'c1', 'A', 'partial', 'revised')
        self.assertEqual(3, len(state['history']))
        self.assertEqual(2, len(state['reviews']))
        self.assertEqual(25, state['metrics']['score'])
        self.assertEqual(2, state['metrics']['reviewer_count'])
        self.assertEqual(1, state['metrics']['disagreements'])

    def test_rounds_keep_fixed_samples_and_survive_restart_without_duplicates(self):
        one = self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'supported', 'round1', one['sample']['id'])
        two = self.store.sample(self.gid, new_round=True, name='Second review')
        self.assertNotEqual(one['sample']['id'], two['sample']['id'])
        self.assertEqual(one['sample']['claim_ids'], self.store.sample_state(self.gid, one['sample']['id'])['sample']['claim_ids'])
        state = self.store.review(self.gid, 'c1', 'B', 'incorrect', 'round2', two['sample']['id'])
        self.assertEqual(50, state['metrics']['score'])
        restarted = Store(self.path)
        self.assertEqual(2, len(restarted.sample_state(self.gid)['rounds']))
        self.assertEqual(2, len(Store(self.path).sample_state(self.gid)['rounds']))
        self.assertEqual(2, len(state['history']))

    def test_later_round_revision_does_not_add_duplicate_vote(self):
        one = self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'supported', '', one['sample']['id'])
        two = self.store.sample(self.gid, new_round=True)
        state = self.store.review(self.gid, 'c1', 'A', 'incorrect', '', two['sample']['id'])
        self.assertEqual(0, state['metrics']['score'])
        self.assertEqual(1, state['metrics']['active_votes'])
        self.assertEqual(2, state['metrics']['history_count'])

    def test_human_changes_correctness_and_scenario_scores_without_llm_rerun(self):
        rid = self.machine()
        before = self.store.run(rid)['report']
        self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'incorrect', 'human evidence')
        after = self.store.run(rid)['report']
        self.assertEqual(100, after['machine_dimensions']['correctness'])
        self.assertEqual(70, after['dimensions']['correctness'])
        self.assertEqual(.3, after['human']['effective_weight'])
        self.assertLess(after['scenarios']['field_review']['score'], before['scenarios']['field_review']['score'])
        self.assertNotEqual(after['score_revision'], before['score_revision'])
        with self.store.connect() as db:
            original = json.loads(db.execute('SELECT report FROM runs WHERE id=?', (rid,)).fetchone()['report'])
        self.assertEqual(100, original['dimensions']['correctness'])
        self.assertNotIn('human', original)

    def test_adding_round_with_same_sample_does_not_dilute_completion(self):
        rid = self.machine()
        one = self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'incorrect', '', one['sample']['id'])
        self.store.sample(self.gid, new_round=True)
        r = self.store.run(rid)['report']
        self.assertEqual(1, r['human']['sampled_unique_claims'])
        self.assertEqual(.3, r['human']['effective_weight'])

    def test_different_claims_not_weighted_by_number_of_reviewers(self):
        rows = [dict(id=i+1, claim_id='c1', reviewer=str(i), verdict='supported') for i in range(8)]
        rows.append(dict(id=9, claim_id='c2', reviewer='other', verdict='incorrect'))
        self.assertEqual(50, human_metrics(rows, 100)['score'])

    def test_uncertain_latest_opinion_is_not_hidden_behind_earlier_support(self):
        self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'supported', '')
        state = self.store.review(self.gid, 'c1', 'A', 'uncertain', '')
        self.assertIsNone(state['metrics']['score'])
        self.assertEqual(1, state['metrics']['uncertain'])

    def test_human_only_review_immediately_has_a_provisional_graph_score(self):
        self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'partial', 'source evidence')
        run = self.store.latest_run(self.gid)
        self.assertEqual('human', run['mode'])
        self.assertEqual(50, run['report']['dimensions']['correctness'])
        self.assertEqual('human_sample_only', run['report']['human']['method'])
        self.assertFalse(run['report']['complete'])
        self.assertIsNotNone(run['report']['scenarios']['field_review']['score'])
        self.assertIsNone(run['report']['scenarios']['field_review']['overall'])

    def test_zero_human_weight_keeps_machine_score(self):
        self.graph['policy']['human_weight'] = 0
        with self.store.connect() as db:
            db.execute('UPDATE graphs SET payload=? WHERE id=?', (json.dumps(self.graph), self.gid))
        rid = self.machine();self.store.sample(self.gid)
        self.store.review(self.gid, 'c1', 'A', 'incorrect', '')
        self.assertEqual(100, self.store.run(rid)['report']['dimensions']['correctness'])

    def test_local_ontology_rerun_does_not_erase_llm_dimensions(self):
        llm_rid = self.machine()
        rid = self.store.start_run(self.gid, 'automatic')
        self.store.update_run(rid, 'completed', evaluate(self.graph))
        report = self.store.run(rid)['report']
        self.assertEqual(llm_rid, report['evidence_run_id'])
        for field in ('specialization', 'granularity', 'completeness', 'correctness', 'coverage'):
            self.assertIsNotNone(report['dimensions'][field])

    def test_v1_database_is_migrated_without_losing_reviews_or_time(self):
        path = Path(self.tmp.name) / 'v1.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE graphs(id TEXT PRIMARY KEY,name TEXT,snapshot_hash TEXT,payload TEXT,original TEXT,created_at TEXT);
                CREATE TABLE samples(graph_id TEXT PRIMARY KEY,seed TEXT,claim_ids TEXT,uncertainty REAL,created_at TEXT);
                CREATE TABLE reviews(id INTEGER PRIMARY KEY,graph_id TEXT,claim_id TEXT,reviewer TEXT,verdict TEXT,note TEXT,created_at TEXT);''')
            db.execute('INSERT INTO graphs VALUES (?,?,?,?,?,?)', ('legacy', 'old', 'hash', json.dumps(self.graph), '{}', 'original-time'))
            db.execute('INSERT INTO samples VALUES (?,?,?,?,?)', ('legacy', '123', '["c1"]', 1, 'original-time'))
            db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?)', (7, 'legacy', 'c1', 'A', 'supported', 'old note', 'original-time'))
            db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?)', (8, 'legacy', 'c1', 'B', 'incorrect', 'other opinion', 'original-time'))
        migrated = Store(path).sample_state('legacy')
        self.assertEqual(1, len(migrated['rounds']))
        self.assertEqual([7, 8], [r['id'] for r in migrated['history']])
        self.assertEqual('original-time', migrated['history'][0]['created_at'])
        self.assertEqual(50, migrated['metrics']['score'])
        self.assertEqual(1, len(Store(path).sample_state('legacy')['rounds']))


class RoundHTTPTests(unittest.TestCase):
    setUp = fixtures.HTTPTests.setUp
    tearDown = fixtures.HTTPTests.tearDown
    request = fixtures.HTTPTests.request
    def test_round_api_and_export_reflect_all_people(self):
        gid = self.request('/api/graphs', {'payload': fixture()})['id']
        one = self.request(f'/api/graphs/{gid}/sample', {})
        two = self.request(f'/api/graphs/{gid}/rounds', {'name': 'another'})
        for reviewer, verdict, rid in [('A', 'supported', one['sample']['id']), ('B', 'incorrect', two['sample']['id'])]:
            self.request(f'/api/graphs/{gid}/reviews', {'claim_id': 'c1', 'reviewer': reviewer, 'verdict': verdict, 'round_id': rid})
        state = self.request(f'/api/graphs/{gid}/sample?round_id=' + one['sample']['id'])
        self.assertEqual(one['sample']['id'], state['sample']['id'])
        self.assertEqual(2, len(state['rounds']))
        self.assertEqual(50, state['metrics']['score'])
        export = self.request(f'/api/graphs/{gid}/export')
        self.assertEqual(2, len(export['human']['history']))
        self.assertEqual(2, len(export['human']['reviews']))


if __name__ == '__main__': unittest.main()
