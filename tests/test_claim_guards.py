import copy
import unittest
import json
from pathlib import Path

from app.claim_guards import audit_claim
from app.claim_audit import fixture_claim, summarize


def claim(sentence, subject='X', object_='Y', predicate='AFFECTS', direction='increased', **extra):
    result = {'subject': {'text': subject}, 'object': {'text': object_, 'process': {'kind': 'activity', 'direction': direction}},
              'predicate': predicate, 'evidence': {'sentence': sentence}, 'negated': False, 'qualifiers': {}}
    result.update(extra)
    return result


class PolarityGuardTests(unittest.TestCase):
    def test_all_21_D1_scenarios(self):
        rows = json.loads((Path(__file__).parent / 'fixtures/kb26_guard_cases.json').read_text())
        self.assertEqual(len(rows), 21)
        for row in rows:
            with self.subTest(claim_id=row['claim_id']):
                self.assertTrue(audit_claim(row['claim_json']))

    def pair(self, rule, positive, negative):
        original = copy.deepcopy(positive)
        self.assertIn(rule, {f['rule_id'] for f in audit_claim(positive)})
        self.assertNotIn(rule, {f['rule_id'] for f in audit_claim(negative)})
        self.assertEqual(positive, original)

    def test_anti_property_not_reduction_of_its_target(self):
        self.pair('V1-ANTI-ATTRIBUTE', claim('X has anti-inflammatory properties.', object_='anti-inflammatory properties', direction='decreased'),
                  claim('X decreased inflammation.', object_='inflammation', direction='decreased'))

    def test_inhibition_operator_is_not_bare_mediator(self):
        self.pair('V1-SUBJECT-OPERATOR', claim('Inhibition of X increases Y.'),
                  claim('Inhibition of X increases Y.', subject='Inhibition of X'))
        honest = claim('Absence of X decreases Y.')
        honest['subject']['process'] = {'kind': 'activity', 'direction': 'decreased'}
        self.assertNotIn('V1-SUBJECT-OPERATOR', {f['rule_id'] for f in audit_claim(honest)})

    def test_decreased_inhibition_event(self):
        positive = claim('Inhibition of X leads to decreased Y.', subject='Inhibition of X', direction='decreased')
        positive['subject']['process'] = {'direction': 'decreased'}
        negative = copy.deepcopy(positive)
        negative['subject']['process']['direction'] = 'increased'
        self.pair('V1-EVENT-OPERATOR', positive, negative)

    def test_passive_protection(self):
        self.pair('V1-PASSIVE-AGENT', claim('X were protected against Y by a membrane.'), claim('X protected against Y.'))

    def test_sensitivity_agent(self):
        self.pair('V1-SENSITIVITY-AGENT', claim('X is sensitive to Y.'), claim('Y is sensitive to X.'))

    def test_exception_names_not_entire_list(self):
        self.pair('V1-EXCEPTION', claim('All but two (Y and Z) were associated with X.', predicate='CORRELATED_WITH'),
                  claim('All but two (A and B) were associated with X.', predicate='CORRELATED_WITH'))

    def test_excluded_object_and_subject_scope(self):
        self.pair('V1-EXCLUDED-OBJECT', claim('X consumes serine-but not Y.', predicate='CONSUMES'),
                  claim('X consumes serine-but not choline.', object_='serine', predicate='CONSUMES'))
        self.assertFalse(audit_claim(claim('Bacteria lacking X decreased Y.', subject='Bacteria lacking X', direction='decreased')))

    def test_unidentified_relationship(self):
        self.pair('V1-NO-RELATION', claim('No relationship between X and Y has yet been identified.', predicate='CORRELATED_WITH'),
                  claim('A relationship between X and Y has been identified.', predicate='CORRELATED_WITH'))

    def test_unassessed_prediction(self):
        self.pair('V1-UNASSESSED-PREDICTION', claim('Insufficient tissue was available to evaluate X predicting Y.', predicate='PREDICTS'),
                  claim('Sufficient tissue was available to evaluate X predicting Y.', predicate='PREDICTS'))

    def test_predicate_negation(self):
        self.pair('V1-NEGATED-PREDICATE', claim('X failed to consume Y.', predicate='CONSUMES'),
                  claim('X failed to consume Y.', predicate='CONSUMES', negated=True))

    def test_contains_direction(self):
        self.pair('V1-CONTAINS-REVERSED', claim('X contains Y motif.', predicate='PART_OF'),
                  claim('Y contains X motif.', predicate='PART_OF'))

    def test_container_direction(self):
        self.pair('V1-CONTAINER-REVERSED', claim('Y in X amended with bile acids.', predicate='PART_OF'),
                  claim('X in Y amended with bile acids.', predicate='PART_OF'))

    def test_unchanged_and_direction_agreement(self):
        self.assertFalse(audit_claim(claim('X caused no change in Y.', direction='unchanged')))
        self.assertFalse(audit_claim(claim('X decreased Y.', direction='decreased')))
        self.assertFalse(audit_claim(claim('X changed A but not Y.', direction='unchanged', result_status='no_effect_reported')))

    def test_fixture_adapter_retains_process_and_does_not_mutate(self):
        row = {'document_id': 2, 'claim_id': 'fact:1', 'subject': {'text':'canonical'}, 'object': {'text':'Y'},
               'predicate': 'AFFECTS', 'evidence_text': 'X decreased Y.',
               'participants': [{'role':'subject','entity':{'text':'X'}}, {'role':'object','entity':{'text':'Y','process':{'direction':'decreased'}}}]}
        before = copy.deepcopy(row)
        self.assertEqual(fixture_claim(row)['object']['process']['direction'], 'decreased')
        self.assertEqual(summarize([row])['hit_count'], 0)
        self.assertEqual(row, before)


if __name__ == '__main__':
    unittest.main()
