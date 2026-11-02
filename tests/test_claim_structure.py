import copy
import json
from pathlib import Path
import unittest

from app.claim_guards import audit_structure,is_methods_section
from app.claims import prepare_claims
from app.schemas import ClaimBundle
from test_review_regressions import candidate,entity


class ClaimStructureTests(unittest.TestCase):
    def ids(self,raw,**context):
        before=copy.deepcopy(raw)
        ids={r['rule_id'] for r in audit_structure(raw,**context)}
        self.assertEqual(raw,before)
        return ids

    def test_actual_ids_override_different_names_and_same_name_conflicts(self):
        raw=candidate('Aspirin affects acetylsalicylic acid.',subject=entity('Aspirin','Compound'),object_=entity('acetylsalicylic acid','Compound'))
        self.assertIn('V6-SELF-LOOP',self.ids(raw,subject_entity_id=5,object_entity_id=5))
        self.assertNotIn('V6-SELF-LOOP',self.ids(raw,subject_entity_id=5,object_entity_id=6))
        raw=candidate('MET affects MET.',subject=entity('MET','Gene'),object_=entity('MET','Protein'))
        self.assertNotIn('V6-SELF-LOOP',self.ids(raw))

    def test_degenerate_comparison_and_distinct_groups(self):
        raw=candidate('Mutant grew less than wild-type.',subject=entity('mutant','MicrobialStrain'),object_=entity('mutant','StudyGroup'),predicate='DIFFERS_BETWEEN')
        self.assertIn('V6-SELF-LOOP',self.ids(raw))
        raw['subject']['text']='growth'
        self.assertNotIn('V6-SELF-LOOP',self.ids(raw))

    def test_method_headings_and_results_heading_boundary(self):
        for section in ['Methods','2.1 Materials and methods','Supplement / Statistical analysis']:
            self.assertTrue(is_methods_section(section))
            self.assertIn('V6-METHOD-RESULT',self.ids(candidate('FMT increased CD8 cells.',result_status='observed_change'),section=section))
        self.assertFalse(is_methods_section('Results: comparison of methods'))
        self.assertNotIn('V6-METHOD-RESULT',self.ids(candidate('FMT increased CD8 cells.',result_status='observed_change'),section='Results'))

    def test_server_section_controls_new_candidate_without_rewriting_quote(self):
        raw=candidate('FMT increased CD8 cells.',result_status='observed_change')
        raw['evidence']['section']='Results'
        result=prepare_claims(ClaimBundle(claims=[raw]),[],{'id':9,'section_title':'Methods','text':raw['evidence']['sentence']},'v')
        self.assertFalse(result._claim_rejections)
        value=result.claims[0]
        self.assertEqual((value.result_status,value.semantic_status),('unknown','pending'))
        self.assertEqual(value.qualifiers['result_interpretation'],'method_description')
        self.assertEqual(value.evidence.sentence,raw['evidence']['sentence'])
        raw['evidence']['section']='Methods'
        value=prepare_claims(ClaimBundle(claims=[raw]),[],{'id':9,'section_title':'Results','text':raw['evidence']['sentence']},'v').claims[0]
        self.assertEqual((value.result_status,value.semantic_status),('observed_change','accepted'))

    def test_controls_standards_and_real_comparison_groups(self):
        for name in ['uninoculated controls','internal standard','calibration reagent']:
            self.assertIn('V6-CONTROL-AGENT',self.ids(candidate('Evidence.',subject=entity(name,'StudyGroup'))))
        self.assertNotIn('V6-CONTROL-AGENT',self.ids(candidate('Healthy controls had CD8 cells.',subject=entity('healthy controls','StudyGroup'))))
        raw=candidate('FMT increased CD8 cells relative to uninoculated controls.',participants=[{'role':'reference_group','entity':entity('uninoculated controls','StudyGroup')}])
        self.assertNotIn('V6-CONTROL-AGENT',self.ids(raw))

    def test_purpose_is_not_a_reported_result(self):
        raw=candidate('To evaluate whether FMT changed CD8 cells, we measured cell counts.')
        self.assertIn('V6-METHOD-INTENT',self.ids(raw))
        raw['evidence']['sentence']='We used flow cytometry to test for CD8 cells and found an increase after FMT.'
        self.assertNotIn('V6-METHOD-INTENT',self.ids(raw))

    def test_literature_reporting_is_not_the_active_agent(self):
        raw=candidate('Meta-analyses have either reported on dysbiosis.',subject=entity('Meta-analyses','Assay'),object_=entity('dysbiosis','Phenotype'))
        self.assertIn('V6-DISCOURSE-EFFECT',self.ids(raw))
        raw['subject']=entity('FMT','Intervention')
        self.assertNotIn('V6-DISCOURSE-EFFECT',self.ids(raw))

    def test_twelve_real_structure_scenarios(self):
        rows=json.loads((Path(__file__).parent/'fixtures/kb26_structure_cases.json').read_text())
        self.assertEqual(len(rows),12)
        for row in rows:
            with self.subTest(claim_id=row['claim_id']):
                self.assertIn(row['expected_rule'],self.ids(row['claim_json']))
