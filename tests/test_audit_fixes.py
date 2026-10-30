"""General failure modes found in the KB-26 source audit, with positive controls."""
import copy
import unittest

from app.claim_semantics import result_view
from app.claims import revalidate_existing_claim
from app.facts import normalized_participants
from app.normalization import entity_identity
from app.schemas import Claim
from test_review_regressions import candidate, entity, prepared


class AuditFixTests(unittest.TestCase):
    def test_negated_production_and_unrelated_unchanged_role_are_not_null_effects(self):
        self.assertEqual(result_view({'predicate':'PRODUCES','negated':True,'qualifiers':{'direction':'unchanged'}}), 'unknown')
        for value in [
            {'predicate':'AFFECTS','negated':True},
            {'predicate':'AFFECTS','qualifiers':{'processes':[{'role':'host','direction':'unchanged'}]}},
            {'predicate':'AFFECTS','subject':{'process':{'direction':'unchanged'}}},
            {'predicate':'DIFFERS_BETWEEN','object':{'process':{'direction':'unchanged'}}},
        ]:
            with self.subTest(value=value):
                self.assertEqual(result_view(value), 'unknown')
        self.assertEqual(result_view({'predicate':'AFFECTS','object':{'process':{'direction':'increased'}}}), 'observed_change')
        self.assertEqual(result_view({'predicate':'DIFFERS_BETWEEN','subject':{'process':{'direction':'unchanged'}}}), 'no_effect_reported')
        comparison=candidate('No difference in Taxon abundance between treated and control.',entity('Taxon','MicrobialTaxon'),entity('treated','StudyGroup'),'DIFFERS_BETWEEN',
            qualifiers={'evidence_type':'experimental','comparison_group':'treated','reference_group':'control','metric':'abundance','direction':'unchanged'})
        normalized=prepared(comparison).claims[0]
        self.assertEqual(normalized.result_status,'no_effect_reported')
        self.assertEqual(normalized.qualifiers['direction'],'unchanged')
        self.assertNotIn('object_direction',normalized.qualifiers)

    def test_not_tested_and_not_detected_do_not_become_unchanged_results(self):
        for sentence, state in [('FMT effects on CD8 cells were not tested.','not_assessed'),
                                ('CD8 cells were not detected after FMT.','not_detected')]:
            raw = candidate(sentence, result_status='no_effect_reported')
            raw['object']['process']['direction'] = 'unchanged'
            result = prepared(raw).claims[0]
            self.assertEqual(result.result_status,'unknown')
            self.assertEqual(result.qualifiers['result_interpretation'], state)
            self.assertEqual(result.semantic_status,'pending')
            self.assertEqual(result_view(result.model_dump()),'unknown')

    def test_no_reduction_does_not_prove_zero_change(self):
        raw=candidate('FMT did not reduce CD8 cells.',negated=True,result_status='no_effect_reported')
        raw['object']['process']['direction']='unchanged'
        claim=prepared(raw).claims[0]
        self.assertTrue(claim.negated)
        self.assertEqual(claim.result_status,'unknown')
        self.assertEqual(result_view(claim.model_dump()),'unknown')
        self.assertEqual(claim.semantic_status,'pending')

    def test_typed_products_are_grounded_and_repeated_roles_are_retained_once(self):
        sentence = 'Bacterium converts choline into TMA and acetate.'
        row = candidate(sentence, entity('Bacterium','MicrobialTaxon'), entity('choline','Compound'),'TRANSFORMS',
            qualifiers={'evidence_type':'experimental','product':[entity('TMA','Compound'),entity('acetate','Compound')]})
        claim = prepared(row).claims[0]
        self.assertEqual([(p.role,p.entity.text) for p in claim.participants],[('product','TMA'),('product','acetate')])
        self.assertEqual(len(normalized_participants(claim)),2)
        self.assertEqual(claim.semantic_status,'accepted')
        row['qualifiers']['product'] = entity('invented product','Compound')
        self.assertFalse(prepared(row).claims)
        row['qualifiers']['product'] = 'TMA'
        claim = prepared(row).claims[0]
        self.assertFalse(claim.participants)
        self.assertEqual(claim.semantic_status,'pending')

    def test_local_strain_codes_are_scoped_but_full_strain_identity_is_global(self):
        for name in ('wild-type','WT','AH18','strain 1','this site'):
            first = entity_identity(name,'MicrobialStrain',document_id=236)
            second = entity_identity(name,'MicrobialStrain',document_id=239)
            self.assertNotEqual(first.canonical_key,second.canonical_key,name)
            self.assertTrue(first.document_scoped,name)
        full = 'Clostridium sporogenes ATCC 15579'
        self.assertEqual(entity_identity(full,'MicrobialStrain',document_id=236).canonical_key,
                         entity_identity(full,'MicrobialStrain',document_id=239).canonical_key)

    def test_unknown_attribution_and_missing_evidence_design_require_review(self):
        row = candidate('FMT increased CD8 cells.',source_kind='unknown')
        self.assertEqual(prepared(row).claims[0].semantic_status,'pending')
        row = candidate('FMT increased CD8 cells.',qualifiers={})
        self.assertEqual(prepared(row).claims[0].semantic_status,'pending')
        self.assertEqual(prepared(candidate('FMT increased CD8 cells.')).claims[0].semantic_status,'accepted')

    def test_reversed_event_and_procedural_process_are_not_confirmed(self):
        row = candidate('FMT increased CD8 cells.', entity('CD8 cells','CellType',{'kind':'abundance','direction':'increased'}),entity('FMT','Intervention'))
        self.assertEqual(prepared(row).claims[0].semantic_status,'pending')
        row = candidate('DNA extraction was followed by sequencing.',entity('DNA extraction','BiologicalProcess'),entity('sequencing','Assay'),'MEASURED_BY')
        self.assertEqual(prepared(row).claims[0].semantic_status,'pending')
        row = candidate('CD8 cells were quantified by flow cytometry.',object_=entity('flow cytometry','Assay'),subject=entity('CD8 cells','CellType'),predicate='MEASURED_BY')
        self.assertEqual(prepared(row).claims[0].semantic_status,'accepted')

    def test_respectively_pairings_require_review(self):
        row = candidate('FMT and drug increased CD8 cells and CD4 cells, respectively.')
        self.assertEqual(prepared(row).claims[0].semantic_status,'pending')

    def test_legacy_hard_error_is_preserved_and_quarantined(self):
        row = candidate('Flow cytometry was used.',entity('Flow cytometry','Assay'),entity('Flow cytometry','Assay'),'MEASURED_BY')
        before = copy.deepcopy(row)
        claim = revalidate_existing_claim(row,[],{'id':2,'text':row['evidence']['sentence']},'v')
        self.assertEqual(row,before)
        self.assertEqual(claim.subject.model_dump(),Claim.model_validate(row).subject.model_dump())
        self.assertEqual(claim.evidence.sentence,row['evidence']['sentence'])
        self.assertEqual(claim.semantic_status,'pending')
        self.assertTrue(any('legacy validation failed' in r for r in claim.review_reasons))

    def test_legacy_without_catalog_retains_identity_and_revalidation_is_idempotent(self):
        raw=candidate('Aspirin targets COX-1.',entity('Aspirin','Drug'),entity('COX-1','Protein'),'TARGETS')
        raw['subject']['normalized_id']='CHEBI:15365'
        chunk={'id':2,'text':raw['evidence']['sentence']}
        first=revalidate_existing_claim(raw,[],chunk,None,use_catalog=False)
        self.assertEqual(first.subject.normalized_id,'CHEBI:15365')
        self.assertFalse(first.subject.pending_merge)
        self.assertEqual(first.semantic_status,'pending')
        second=revalidate_existing_claim(first.model_dump(),[],chunk,None,use_catalog=False)
        self.assertEqual(first.model_dump(),second.model_dump())


if __name__ == '__main__':
    unittest.main()
