import copy
import json
from pathlib import Path
import unittest

from app.claim_guards import audit_entity_text,ENTITY_WORD_LIMIT
from app.normalization import entity_identity
from test_review_regressions import candidate,entity


class EntityHygieneTests(unittest.TestCase):
    def ids(self,raw):
        before=copy.deepcopy(raw)
        result={f['rule_id'] for f in audit_entity_text(raw)}
        self.assertEqual(raw,before)
        return result

    def test_sentence_clause_is_not_an_entity_name(self):
        raw=candidate('Acetate activates cells.',subject=entity('acetate is produced by enteric bacteria','Metabolite'))
        self.assertIn('V5-ENTITY-CLAUSE',self.ids(raw))
        raw['subject']['text']='acetate production by enteric bacteria'
        self.assertNotIn('V5-ENTITY-CLAUSE',self.ids(raw))

    def test_word_limit_and_auxiliary_entities(self):
        raw=candidate('Evidence.',participants=[{'role':'assay','entity':entity(' '.join(['word']*(ENTITY_WORD_LIMIT+1)),'Assay')}])
        self.assertIn('V5-ENTITY-LENGTH',self.ids(raw))
        raw['participants'][0]['entity']['text']=' '.join(['word']*ENTITY_WORD_LIMIT)
        self.assertNotIn('V5-ENTITY-LENGTH',self.ids(raw))

    def test_ellipsis_and_midword_prefix_are_reviewed_without_repair(self):
        raw=candidate('Spontaneous BA degradation or transformation was assessed.',qualifiers={'assay':'spontaneous BA degradation or transf'})
        self.assertIn('V5-TRUNCATED-SLOT',self.ids(raw))
        raw['qualifiers']['assay']='spontaneous BA degradation or transformation'
        self.assertNotIn('V5-TRUNCATED-SLOT',self.ids(raw))
        raw['qualifiers']['assay']='spontaneous BA…'
        self.assertIn('V5-TRUNCATED-SLOT',self.ids(raw))

    def test_plural_and_complete_slot_are_not_truncation(self):
        raw=candidate('Uninoculated controls were assessed.',qualifiers={'group':'uninoculated control'})
        self.assertNotIn('V5-TRUNCATED-SLOT',self.ids(raw))
        raw['qualifiers']['group']='test and'
        self.assertIn('V5-TRUNCATED-SLOT',self.ids(raw))

    def test_safe_separator_and_charge_variants_share_identity(self):
        for names,type_ in [(['sodium butyrate','sodium-butyrate','sodium_butyrate'],'Compound'),
                            (['gut microbiota','gut-microbiota','gut_microbiota'],'MicrobialCommunity'),
                            (['NAD+','NAD +'],'Metabolite')]:
            self.assertEqual(len({entity_identity(name,type_).canonical_key for name in names}),1)

    def test_stereochemistry_strains_and_spelling_are_not_guessed(self):
        for a,b,type_ in [('D-lactate','L-lactate','Metabolite'),('cis-platin','trans-platin','Drug'),
                         ('strain A-1','strain A_1','MicrobialStrain'),('IFN-γ','INF-γ','Protein'),
                         ('butyrate','utyrate','Metabolite'),('blood brain','bloodbrain','AnatomicalStructure')]:
            self.assertNotEqual(entity_identity(a,type_).canonical_key,entity_identity(b,type_).canonical_key)
        self.assertNotEqual(entity_identity('MET','Protein').canonical_key,entity_identity('MET','Gene').canonical_key)

    def test_five_real_clause_corruptions(self):
        rows=json.loads((Path(__file__).parent/'fixtures/kb26_entity_hygiene_cases.json').read_text())
        self.assertEqual(len(rows),5)
        for row in rows:
            with self.subTest(claim_id=row['claim_id']):
                self.assertIn('V5-ENTITY-CLAUSE',self.ids(row['claim_json']))
