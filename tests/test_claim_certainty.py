import unittest
import json
from pathlib import Path

from app.claim_guards import audit_certainty, certainty_evidence
from app.claim_semantics import normalize_result, result_view
from app.schemas import Claim
from test_review_regressions import candidate, entity, prepared


class CertaintyGuardTests(unittest.TestCase):
    def test_six_real_D2_scenarios(self):
        rows=json.loads((Path(__file__).parent/'fixtures/kb26_certainty_cases.json').read_text())
        self.assertEqual(len(rows),6)
        for row in rows:
            with self.subTest(claim_id=row['claim_id']):
                raw=row['claim_json']
                self.assertTrue(audit_certainty(raw))
                value=Claim.model_validate({**raw,'confidence':.9})
                normalize_result(value,[])
                expected=row['expected_status']
                self.assertEqual(value.result_status,expected)
                self.assertEqual(value.evidence.sentence,raw['evidence']['sentence'])
    def findings(self, raw):
        return {r['rule_id'] for r in audit_certainty(raw)}

    def test_speculative_observed_and_honest_unknown(self):
        raw = candidate('FMT increased CD8 cells.', speculative=True, result_status='observed_change')
        self.assertIn('V2-HEDGE-RESULT',self.findings(raw))
        self.assertEqual(result_view(raw),'unknown')
        raw['result_status']='unknown'
        self.assertNotIn('V2-HEDGE-RESULT',self.findings(raw))
        raw['object']['process']['direction']='unchanged'
        self.assertEqual(result_view(raw),'no_effect_reported')

    def test_lexical_hedge_downgrades_without_rewriting_evidence(self):
        raw = candidate('FMT may increase CD8 cells.',result_status='observed_change')
        self.assertIn('V2-HEDGE-RESULT',self.findings(raw))
        value=Claim.model_validate(raw)
        normalize_result(value,[])
        self.assertEqual(value.result_status,'unknown')
        self.assertEqual(result_view(value.model_dump()),'unknown')
        self.assertEqual(value.evidence.sentence,raw['evidence']['sentence'])
        honest = candidate('FMT increased CD8 cells, which may explain tumor control.',result_status='observed_change')
        self.assertNotIn('V2-HEDGE-RESULT',self.findings(honest))
        self.assertNotIn('V2-HEDGE-RESULT',self.findings(candidate('In May 2019 FMT increased CD8 cells.',result_status='observed_change')))

    def test_explanatory_hedge_does_not_erase_observed_infiltration(self):
        raw = candidate('Tumor growth was likely due to the increase in CD8 cells infiltration which was observed in the patient.', result_status='observed_change')
        self.assertNotIn('V2-HEDGE-RESULT',self.findings(raw))
        raw['evidence']['sentence']='FMT likely increased CD8 cells.'
        self.assertIn('V2-HEDGE-RESULT',self.findings(raw))

    def test_p_value_and_direction_are_independent(self):
        for source in ('FMT slightly decreased CD8 cells (P=0.091).','FMT decreased CD8 cells (p > 0.05).','FMT decreased CD8 cells (non-significant).'):
            raw=candidate(source,result_status='observed_change')
            raw['object']['process']['direction']='decreased'
            self.assertIn('V2-SIGNIFICANCE',self.findings(raw))
            value=Claim.model_validate(raw)
            normalize_result(value,[])
            self.assertEqual(value.result_status,'not_significant')
            self.assertEqual(value.object.process.direction,'decreased')
            self.assertEqual(value.qualifiers['result_interpretation'],'not_significant')
            if '0.091' in source:self.assertEqual(value.qualifiers['p_value'],.091)
        self.assertNotIn('V2-SIGNIFICANCE',self.findings(candidate('FMT increased CD8 cells (P=0.01).', result_status='observed_change')))
        resolved=prepared(candidate('FMT did not significantly affect CD8 cells.')).claims[0]
        self.assertEqual(resolved.semantic_status,'accepted')
        self.assertFalse(any('V2-' in r for r in resolved.review_reasons))

    def test_invalid_p_or_lost_exponent_is_not_a_large_probability(self):
        raw=candidate('FMT increased CD8 cells (P=7.48 -12).',result_status='observed_change')
        self.assertFalse(certainty_evidence(raw)['not_significant'])
        self.assertFalse(certainty_evidence(candidate('FMT increased CD8 cells (P=7.48e-12).'))['not_significant'])
        self.assertFalse(certainty_evidence(candidate('FMT increased CD8 cells (P=0.5 × 10-3).'))['not_significant'])

    def test_correlation_significance_is_preserved_in_existing_qualifier(self):
        raw=candidate('A nonsignificant but negative correlation with Bacteroidales.',subject=entity('Bacteroidales','MicrobialTaxon'),
                      object_=entity('CD8 cells','CellType'),predicate='CORRELATED_WITH')
        self.assertIn('V2-SIGNIFICANCE',self.findings(raw))
        value=Claim.model_validate(raw)
        normalize_result(value,[])
        self.assertEqual(value.qualifiers['result_interpretation'],'not_significant')
        self.assertNotIn('V2-SIGNIFICANCE',self.findings(value.model_dump()))

    def test_unmeasurable_is_not_unchanged_and_other_arm_is_not_borrowed(self):
        raw=candidate('CD8 cells could not be measured.',result_status='no_effect_reported')
        raw['object']['process']['direction']='unchanged'
        self.assertIn('V2-UNMEASURED-NULL',self.findings(raw))
        value=Claim.model_validate(raw)
        normalize_result(value,[])
        self.assertEqual((value.result_status,value.object.process.direction),('unknown','unspecified'))
        self.assertEqual(value.qualifiers['result_interpretation'],'not_assessed')
        self.assertEqual(value.evidence.sentence,raw['evidence']['sentence'])
        raw['evidence']['sentence']='CD8 cells were unchanged, but IL-8 was not assessed.'
        self.assertNotIn('V2-UNMEASURED-NULL',self.findings(raw))

    def test_trend_requires_significance_annotation_but_does_not_invent_p(self):
        raw=candidate('FMT caused an increasing trend in CD8 cells.',result_status='observed_change')
        self.assertIn('V2-TREND-SIGNIFICANCE',self.findings(raw))
        raw['qualifiers']['p_value']=.08
        self.assertNotIn('V2-TREND-SIGNIFICANCE',self.findings(raw))

    def test_deliberate_contracted_null_is_resolved(self):
        raw=candidate("Even if CD8 cells doesn't change, FMT impacts other cells.",result_status='unknown',
                      qualifiers={'result_interpretation':'unresolved_negative'})
        raw['object']['process']['direction']='unchanged'
        self.assertIn('V2-RESOLVED-NULL',self.findings(raw))
        value=Claim.model_validate(raw)
        normalize_result(value,[])
        self.assertEqual(value.result_status,'no_effect_reported')
        self.assertEqual(result_view(value.model_dump()),'no_effect_reported')
        self.assertNotIn('V2-RESOLVED-NULL',self.findings(value.model_dump()))
