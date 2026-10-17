"""Microbiome extraction and graph semantics with synthetic text and no live LLM."""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.catalog import merge_catalog, prepare_catalog
from app.claims import prepare_claims
from app.entity_types import TYPE_DEFINITIONS
from app.facts import fact_qualifiers
from app.graph_store import _claim_projection, _merge_entities
from app.normalization import entity_identity, normalize_entity_type
from app.predicate_registry import PREDICATES
from app.schemas import CatalogBundle, CatalogEntity, Claim, ClaimBundle


def entity(text, type_, **extra):
    return dict(text=text, type=type_, mention=text, **extra)


def assertion(subject, predicate, object_, sentence, **extra):
    return dict(subject=subject, predicate=predicate, object=object_,
                evidence={"sentence": sentence}, confidence=.9, **extra)


def prepare(rows, source, entries=()):
    return prepare_claims(ClaimBundle.model_validate({"claims": rows}), entries,
                          {"id": 1, "text": source}, "microbiome-test")


class MicrobiomeTests(unittest.TestCase):
    def test_new_types_keep_biomedical_vocabulary_and_local_feature_identity(self):
        for name in TYPE_DEFINITIONS:
            self.assertEqual(normalize_entity_type(name), name)
        self.assertEqual(normalize_entity_type('microbial_strain'), 'MicrobialStrain')
        self.assertEqual(normalize_entity_type('OTU'), 'SequenceFeature')
        self.assertEqual(normalize_entity_type('invented'), 'Other')
        for name, type_ in [('ASV1', 'SequenceFeature'), ('controls', 'StudyGroup'),
                            ('stool sample', 'Biospecimen'), ('MAG_1', 'Genome')]:
            self.assertNotEqual(entity_identity(name, type_, document_id=1).canonical_key,
                                entity_identity(name, type_, document_id=2).canonical_key)
        self.assertNotEqual(entity_identity('B. fragilis', 'MicrobialTaxon').canonical_key,
                            entity_identity('B. fragilis NCTC 9343', 'MicrobialStrain').canonical_key)
        self.assertEqual(entity_identity('butyrate', 'Metabolite').canonical_key,
                         entity_identity('butyrate', 'Compound').canonical_key)

    def test_every_aspect_survives_catalog_binding_and_fact_projection(self):
        cases = {
            'expression': ('IL-6', 'Protein', 'expression'),
            'secretion': ('IL-6', 'Protein', 'secretion'),
            'activity': ('IL-6', 'Protein', 'activity'),
            'abundance': ('Bacteroides', 'MicrobialTaxon', 'relative abundance'),
            'prevalence': ('Bacteroides', 'MicrobialTaxon', 'prevalence'),
            'growth': ('Bacteroides', 'MicrobialTaxon', 'growth'),
            'colonization': ('Bacteroides', 'MicrobialTaxon', 'colonization'),
            'production': ('butyrate', 'Metabolite', 'production'),
            'degradation': ('inulin', 'DietaryComponent', 'degradation'),
            'conversion': ('cholate', 'Metabolite', 'conversion'),
            'concentration': ('butyrate', 'Metabolite', 'concentration'),
            'diversity': ('gut microbiota', 'MicrobialCommunity', 'diversity'),
        }
        for kind, (name, type_, wording) in cases.items():
            with self.subTest(kind=kind):
                mention = f'the {wording} of {name}'
                source = f'FMT decreased {mention} in an experiment.'
                catalog = prepare_catalog(CatalogBundle(entities=[
                    CatalogEntity(text='FMT', type='Intervention', evidence=source),
                    CatalogEntity(text=name, type=type_, mention=mention, evidence=source,
                                  process={'kind':kind, 'direction':'decreased'}),
                ]), {'text':source})
                entries = merge_catalog([(e, {}) for e in catalog.entities], {}, 1)
                row = assertion(entity('FMT', 'Intervention'), 'AFFECTS',
                    entity(name, type_, process={'kind':kind, 'direction':'decreased'}), source,
                    qualifiers={'evidence_type':'experimental', 'metric':wording})
                row['object']['mention'] = mention
                result = prepare([row], source, entries)
                self.assertFalse(result._claim_rejections)
                claim = result.claims[0]
                self.assertEqual(claim.object.text, name)
                self.assertTrue(claim.object.directory_id)
                self.assertFalse(claim.object.pending_merge)
                self.assertEqual(claim.object.mention, mention)
                self.assertEqual(fact_qualifiers(claim)['processes'],
                                 [{'role':'object', 'kind':kind, 'direction':'decreased'}])

    def test_direction_alias_is_audited_without_inventing_an_aspect(self):
        source = 'FMT reduces butyrate.'
        row = assertion(entity('FMT', 'Intervention'), 'reduces', entity('butyrate', 'Metabolite'), source)
        result = prepare([row], source)
        self.assertEqual(result.claims[0].predicate, 'AFFECTS')
        self.assertEqual(result.claims[0].qualifiers['object_direction'], 'decreased')
        self.assertIsNone(result.claims[0].object.process)
        self.assertTrue(any('aspect is unspecified' in r for r in result.claims[0].review_reasons))
        self.assertTrue(any(r['field'] == 'predicate' and r['original'] == 'reduces' for r in result._claim_repairs))
        self.assertEqual(row['predicate'], 'reduces')

    def test_invalid_endpoints_unknown_predicates_and_cooccurrence_are_quarantined(self):
        source = 'Bacteroides and Roseburia co-occurred.'
        base = assertion(entity('Bacteroides', 'MicrobialTaxon'), 'ASSOCIATED_WITH',
                         entity('Roseburia', 'MicrobialTaxon'), source, qualifiers={'evidence_type':'cooccurrence'})
        competing, invented, bad_membership = [copy.deepcopy(base) for _ in range(3)]
        competing['predicate'] = 'COMPETES_WITH'
        invented['predicate'] = 'INCREASES_IN_MOUSE_STOOL'
        bad_membership['predicate'] = 'MEMBER_OF'  # A taxon is not a community.
        result = prepare([base, competing, invented, bad_membership], source)
        self.assertEqual([c.predicate for c in result.claims], ['ASSOCIATED_WITH'])
        self.assertEqual(len(result._claim_rejections), 3)
        self.assertIn('evidence_type', result._claim_rejections[0]['error'])
        self.assertIn('Unregistered', result._claim_rejections[1]['error'])
        self.assertIn('object type', result._claim_rejections[2]['error'])

    def test_computational_function_stays_predicted_and_cannot_become_ecological_mechanism(self):
        source = 'Roseburia was predicted to produce butyrate.'
        row = assertion(entity('Roseburia', 'MicrobialTaxon'), 'PRODUCES', entity('butyrate', 'Metabolite'), source,
                        qualifiers={'evidence_type':'computational_prediction', 'knowledge_level':'predicted'})
        wrong = copy.deepcopy(row)
        wrong['qualifiers']['knowledge_level'] = 'observed'
        result = prepare([row, wrong], source)
        self.assertEqual(len(result.claims), 1)
        self.assertEqual(result.claims[0].qualifiers['knowledge_level'], 'predicted')
        self.assertEqual(len(result._claim_rejections), 1)

    def test_group_difference_requires_both_grounded_groups_metric_and_direction(self):
        source = 'Bacteroides abundance was higher in patients than in controls.'
        row = assertion(entity('Bacteroides', 'MicrobialTaxon', process={'kind':'abundance', 'direction':'increased'}),
            'DIFFERS_BETWEEN', entity('patients', 'StudyGroup'), source,
            participants=[{'role':role, 'entity':entity(label, 'StudyGroup')} for role, label in
                          [('comparison_group', 'patients'), ('reference_group', 'controls')]],
            qualifiers={'comparison_group':'patients', 'reference_group':'controls',
                        'metric':'abundance', 'direction':'increased', 'evidence_type':'observational'})
        bad_rows = []
        for field in ['comparison_group', 'reference_group', 'metric', 'direction']:
            bad = copy.deepcopy(row)
            del bad['qualifiers'][field]
            bad_rows.append(bad)
        missing_participant = copy.deepcopy(row)
        missing_participant['participants'].pop()
        missing_participant['qualifiers']['reference_group'] = 'unreported group'
        bad_rows.append(missing_participant)
        wrong_direction = copy.deepcopy(row)
        wrong_direction['qualifiers']['direction'] = 'decreased'
        bad_rows.append(wrong_direction)
        result = prepare([row, *bad_rows], source)
        self.assertEqual(len(result.claims), 1)
        self.assertEqual(len(result._claim_rejections), len(bad_rows))
        self.assertEqual(fact_qualifiers(result.claims[0])['processes'][0]['role'], 'subject')

        # Existing models can supply literal qualifier labels without repeating
        # the group entities; the ordinary grounding path materializes them.
        compact = copy.deepcopy(row)
        compact['participants'] = []
        compact['subject']['process'].pop('direction')
        result = prepare([compact], source)
        self.assertFalse(result._claim_rejections)
        self.assertEqual({p.role for p in result.claims[0].participants}, {'comparison_group', 'reference_group'})
        self.assertEqual(result.claims[0].subject.process.direction, 'increased')

    def test_signed_correlation_is_never_normalized_to_an_effect(self):
        source = 'Bacteroides was negatively correlated with inflammation.'
        row = assertion(entity('Bacteroides', 'MicrobialTaxon'), 'negatively correlated with',
                        entity('inflammation', 'Phenotype'), source,
                        qualifiers={'evidence_type':'observational'})
        result = prepare([row], source)
        self.assertFalse(result._claim_rejections)
        self.assertEqual(result.claims[0].predicate, 'CORRELATED_WITH')
        self.assertEqual(result.claims[0].qualifiers['association_direction'], 'negative')
        self.assertIsNone(result.claims[0].object.process)

    def test_classification_and_community_membership_cannot_use_physical_composition(self):
        source = 'Bacteroides belongs to gut microbiota.'
        rows = [assertion(entity('Bacteroides', 'MicrobialTaxon'), pred,
                          entity('gut microbiota', 'MicrobialCommunity'), source)
                for pred in ['MEMBER_OF', 'SUBCLASS_OF', 'PART_OF']]
        result = prepare(rows, source)
        self.assertEqual([c.predicate for c in result.claims], ['MEMBER_OF'])
        self.assertEqual(len(result._claim_rejections), 2)

    def test_repeated_participant_roles_keep_process_targets_and_relation_identity(self):
        source = 'FMT affects gut microbiota, butyrate concentration and acetate production.'
        row = assertion(entity('FMT', 'Intervention'), 'AFFECTS', entity('gut microbiota', 'MicrobialCommunity'), source,
            participants=[{'role':'metabolite', 'entity':entity(name, 'Metabolite', process={'kind':kind})}
                          for name, kind in [('butyrate', 'concentration'), ('acetate', 'production')]])
        claim = Claim.model_validate(row)
        ids = {'FMT':1, 'gut microbiota':2, 'butyrate':3, 'acetate':4}
        def project(value):
            with patch('app.graph_store.lock_graph_projection'), patch('app.graph_store.upsert_entity',
                    side_effect=lambda cur, identity, **kwargs: ids[identity.canonical_name]):
                return _claim_projection(MagicMock(), value, document_id=1)
        projection = project(claim)
        self.assertEqual({p['entity_id']:p['kind'] for p in projection['qualifiers']['processes']},
                         {3:'concentration', 4:'production'})
        claim.participants.reverse()
        self.assertEqual(project(claim)['qualifiers_hash'], projection['qualifiers_hash'])
        claim.participants[0].entity.process.kind = 'concentration'
        self.assertNotEqual(project(claim)['qualifiers_hash'], projection['qualifiers_hash'])

    def test_new_aspects_do_not_erase_strain_identity(self):
        source = 'FMT decreased the abundance of Bacteroides fragilis NCTC 9343.'
        row = assertion(entity('FMT', 'Intervention'), 'AFFECTS',
                        entity('Bacteroides fragilis', 'MicrobialStrain', process={'kind':'abundance'}), source)
        row['object']['mention'] = 'the abundance of Bacteroides fragilis NCTC 9343'
        result = prepare([row], source)
        self.assertIn('NCTC 9343', result.claims[0].object.text)
        self.assertTrue(result.claims[0].object.pending_merge)

    def test_entity_merge_rewires_process_targets_in_relations_and_complete_facts(self):
        qualifiers = {'participants':[['metabolite', 3]], 'processes':[
            {'role':'metabolite', 'entity_id':3, 'kind':'concentration', 'direction':'increased'}]}
        relation = dict(id=20, subject_entity_id=1, object_entity_id=2, predicate='AFFECTS',
                        qualifiers=copy.deepcopy(qualifiers), negated=False, speculative=False)
        fact = dict(predicate='AFFECTS', qualifiers=copy.deepcopy(qualifiers), negated=False, speculative=False)
        cur = MagicMock()
        cur.fetchall.side_effect = [[{'id':10, 'document_id':1}], [relation],
                                   [{'role':'metabolite', 'entity_id':4}]]
        cur.fetchone.side_effect = [{'id':21}, fact]
        _merge_entities(cur, {'id':4, 'normalized_id':None},
                        [{'id':3, 'normalized_id':None, 'entity_type':'Metabolite'}])
        inserts = [c.args[1] for c in cur.execute.call_args_list if 'INSERT INTO relations' in c.args[0]]
        updates = [c.args[1] for c in cur.execute.call_args_list if 'UPDATE facts SET fact_key=' in c.args[0]]
        for projected in [inserts[0][3].obj, updates[0][1].obj]:
            self.assertEqual(projected['processes'][0]['entity_id'], 4)
            self.assertEqual(projected['participants'], [('metabolite', 4)])

    def test_registry_api_and_prompts_share_vocabulary_and_keep_existing_model_transport(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import main, llm
        self.assertEqual(main.entity_types()['types'], TYPE_DEFINITIONS)
        registry = main.predicate_types()
        self.assertEqual(registry['predicates'], PREDICATES)
        self.assertTrue(any(r.path == '/predicate-types' for r in main.app.routes))
        prompt = llm.build_extraction_messages('Results', 'synthetic source')[1]['content']
        for name in PREDICATES:
            self.assertIn(name + ':', prompt)
        raw = json.dumps({'entities':[]})
        with patch.object(llm, 'chat_completion', return_value=raw) as chat:
            llm.extract_catalog('Results', 'synthetic source')
        self.assertIn('MicrobialStrain', chat.call_args.args[0][1]['content'])
        response = MagicMock()
        response.choices[0].message.content = '{"claims":[]}'
        response.choices[0].finish_reason = 'stop'
        with patch.object(llm.client.chat.completions, 'create', return_value=response) as create:
            llm.extract_claims('Results', 'synthetic source')
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['model'], llm.LLM_MODEL)
        self.assertEqual(kwargs['max_tokens'], llm.CLAIMS_MAX_OUTPUT_TOKENS)
        self.assertNotIn('tools', kwargs)
        self.assertNotIn('response_format', kwargs)
        create.assert_called_once()
