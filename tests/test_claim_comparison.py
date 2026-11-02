import copy
import json
from pathlib import Path
import unittest

from app.claim_guards import audit_comparison
from test_review_regressions import candidate,entity


def contrast(sentence,**qualifiers):
    return candidate(sentence,subject=entity('taxon','MicrobialTaxon'),object_=entity('treated','StudyGroup'),
                     predicate='DIFFERS_BETWEEN',qualifiers={'metric':'OD','direction':'increased',
                     'comparison_group':'treated','reference_group':'control',**qualifiers})


class ComparisonGuardTests(unittest.TestCase):
    def ids(self,raw):
        before=copy.deepcopy(raw)
        result={f['rule_id'] for f in audit_comparison(raw)}
        self.assertEqual(raw,before)
        return result

    def test_groups_are_required_and_distinct(self):
        self.assertIn('V4-COMPARISON-ARMS',self.ids(contrast('OD differed.',reference_group='')))
        self.assertIn('V4-SAME-GROUP',self.ids(contrast('OD differed.',reference_group=' TREATED ')))
        self.assertFalse(self.ids(contrast('OD differed between treated and control.')))

    def test_explicit_than_requires_comparison_slots(self):
        raw=candidate('taxon produces more product at 500 μM than at 100 μM.',predicate='PRODUCES')
        self.assertIn('V4-COMPARISON-ARMS',self.ids(raw))
        raw['qualifiers'].update(comparison_group='500 μM',reference_group='100 μM')
        self.assertNotIn('V4-COMPARISON-ARMS',self.ids(raw))
        self.assertFalse(self.ids(candidate('Sulfate reduction accounts for more than 50% of carbon mineralization.',predicate='PARTICIPATES_IN')))

    def test_measured_values_must_be_preserved_and_figures_are_not_values(self):
        raw=contrast('OD in treated was 0.03, compared to control at 0.15 (Fig. 3).')
        self.assertIn('V4-NUMERIC-UNBOUND',self.ids(raw))
        raw['qualifiers'].update(value=.03,reference_value=.15)
        self.assertNotIn('V4-NUMERIC-UNBOUND',self.ids(raw))
        self.assertFalse(self.ids(contrast('OD was higher in treated than control (p=0.0012; n=15; Fig. 2).')))
        self.assertFalse(self.ids(contrast('OD was higher in treated (p-value=0.001) than control (p-value=0.006).')))

    def test_numeric_direction_only_when_all_pairings_agree(self):
        raw=contrast('OD in treated was 0.03 and 0.12, with control OD at 0.15.',value=[.03,.12],reference_value=.15)
        self.assertIn('V4-NUMERIC-DIRECTION',self.ids(raw))
        raw['qualifiers']['direction']='decreased'
        self.assertNotIn('V4-NUMERIC-DIRECTION',self.ids(raw))
        raw['evidence']['sentence']='OD in treated was 0.03 and 0.20, with control OD at 0.15.'
        self.assertNotIn('V4-NUMERIC-DIRECTION',self.ids(raw))

    def test_concentration_arms_are_not_effect_sizes(self):
        raw=contrast('taxon had higher OD at 100 μM than at 500 μM (Fig. 3).',comparison_group='100 μM',reference_group='500 μM')
        self.assertFalse(self.ids(raw))

    def test_consumed_partner_cannot_be_bound_as_product(self):
        raw=candidate('X consumes both rutin and quercetin.',object_=entity('quercetin','DietaryComponent'),predicate='CONSUMES',
                      participants=[{'role':'product','entity':entity('rutin','DietaryComponent')}])
        self.assertIn('V4-PRODUCT-ROLE',self.ids(raw))
        raw['evidence']['sentence']='X consumes both rutin and quercetin to produce acetate.'
        raw['participants'][0]['entity']['text']='acetate'
        raw['participants'][0]['entity']['mention']='acetate'
        self.assertNotIn('V4-PRODUCT-ROLE',self.ids(raw))

    def test_direction_requires_compatible_explicit_units(self):
        raw=contrast('OD in treated was 1.0 mg, with control OD at 2.0 μg.',value=1,reference_value=2)
        self.assertNotIn('V4-NUMERIC-DIRECTION',self.ids(raw))
        raw['evidence']['sentence']='OD in treated was 10%, with control OD at 20%.'
        self.assertIn('V4-NUMERIC-DIRECTION',self.ids(raw))

    def test_audit_comparison_scenarios(self):
        rows=json.loads((Path(__file__).parent/'fixtures/kb26_comparison_cases.json').read_text())
        for row in rows:
            with self.subTest(claim_id=row['claim_id']):
                ids=self.ids(row['claim_json'])
                for expected in row['expected_rules']:self.assertIn(expected,ids)
                if not row['expected_rules']:self.assertFalse(ids)
