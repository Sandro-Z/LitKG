"""Source-grounded proteomics contracts, with no external model or database."""
import copy
import json
import os
from typing import get_args
import unittest
from unittest.mock import MagicMock, patch

from app.catalog import CatalogValidationError, merge_catalog, prepare_catalog, source_has_identifier
from app.claims import prepare_claims
from app.entity_types import TYPE_DEFINITIONS, type_is_allowed
from app.facts import fact_qualifiers
from app.graph_store import _claim_projection, _merge_entities
from app.normalization import entity_identity, identity_identifier_key, normalize_entity_type, normalize_external_id
from app.predicate_registry import PREDICATES
from app.processes import PROCESS_DEFINITIONS, ProcessKind
from app.schemas import CatalogBundle, Claim, ClaimBundle


def entity(name, kind, **extra):
    return dict(text=name, mention=name, type=kind, **extra)


def assertion(subject, predicate, object_, source, **extra):
    return dict(subject=subject, predicate=predicate, object=object_,
                qualifiers={'evidence_type': 'experimental'}, evidence={'sentence': source},
                confidence=.9, **extra)


def prepare(rows, source):
    return prepare_claims(ClaimBundle(claims=rows), [], {'id': 1, 'text': source}, 'test')


class ProteomicsTests(unittest.TestCase):
    def test_aliases_and_parent_constraints_do_not_merge_identity_families(self):
        for label, expected in [('protein isoform', 'Proteoform'), ('PTM', 'ProteinModification'),
                                ('peptidoform', 'Peptide'), ('organelle', 'SubcellularLocation')]:
            self.assertEqual(normalize_entity_type(label), expected)
        self.assertTrue(type_is_allowed('Proteoform', ['Protein']))
        self.assertFalse(type_is_allowed('ProteinGroup', ['Protein']))
        identities = [entity_identity('AKT1', kind, 'UniProt:P31749').canonical_key
                      for kind in ['Gene', 'Protein', 'Peptide', 'Proteoform', 'ProteinGroup']]
        self.assertEqual(len(set(identities)), 5)
        for unsupported in ['MicrobialTaxon', 'MicrobialStrain', 'Microbiome', 'DiversityMetric']:
            self.assertNotIn(unsupported, TYPE_DEFINITIONS)
        for unsupported in ['COLONIZES', 'CROSS_FEEDS', 'COMPETES_WITH', 'INFECTS']:
            self.assertNotIn(unsupported, PREDICATES)

    def test_forms_and_sequences_remain_distinct_even_with_shared_parent_accession(self):
        for kind, names in [
            ('Proteoform', ['AKT1 isoform 1', 'AKT1 isoform 2', 'AKT1 S473 phosphorylated', 'AKT1 T308 phosphorylated']),
            ('Peptide', ['PEPTIDEK', 'PEPTLDEK', 'PEPT[UNIMOD:21]IDEK', 'PEPTIDEK[UNIMOD:1]']),
            ('ModificationSite', ['AKT1 S473', 'AKT2 S473', 'AKT1 T308']),
        ]:
            values = [entity_identity(name, kind, 'UniProt:P31749') for name in names]
            self.assertEqual(len({v.canonical_key for v in values}), len(names))
            self.assertEqual(len({identity_identifier_key(v) for v in values}), len(names))

    def test_study_local_labels_are_document_scoped(self):
        for kind, label in [('ProteinGroup', 'group 1'), ('StudyGroup', 'controls'),
                            ('Biospecimen', 'serum'), ('Proteome', 'plasma proteome'),
                            ('Peptide', 'peptide 1'), ('Proteoform', 'proteoform 1')]:
            a = entity_identity(label, kind, document_id=1)
            b = entity_identity(label, kind, document_id=2)
            self.assertTrue(a.document_scoped, kind)
            self.assertNotEqual(a.canonical_key, b.canonical_key)
        for kind, label in [('ProteinGroup', 'group 1'), ('Peptide', 'peptide 1'), ('Proteoform', 'proteoform 1')]:
            values = [entity_identity(label, kind, 'UniProt:P31749', document_id=doc) for doc in [1, 2]]
            self.assertTrue(all(v.document_scoped and v.normalized_id is None for v in values))
            self.assertNotEqual(values[0].canonical_key, values[1].canonical_key)

    def test_explicit_modification_ids_normalize_and_still_require_source(self):
        for label, expected in [('psi-mod: 00696', 'MOD:00696'), ('MOD_00696', 'MOD:00696'),
                                ('unimod: 021', 'UNIMOD:21'), ('PSI-MS:1000040', 'MS:1000040')]:
            self.assertEqual(normalize_external_id(label), expected)
            self.assertTrue(source_has_identifier('Reported identifier ' + label, expected))
        source = 'Phosphorylation was recorded as Unimod:21.'
        row = entity('Phosphorylation', 'ProteinModification', normalized_id='UNIMOD:21', evidence=source)
        self.assertEqual(len(prepare_catalog(CatalogBundle(entities=[row]), {'text': source}).entities), 1)
        row['normalized_id'] = 'UNIMOD:35'
        repaired = prepare_catalog(CatalogBundle(entities=[row]), {'text': source})
        self.assertIsNone(repaired.entities[0].normalized_id)
        self.assertTrue(any(r['field'] == 'normalized_id' for r in repaired._catalog_repairs))

    def test_directory_does_not_merge_modified_forms_via_parent_alias(self):
        rows = [entity(name, 'Proteoform', normalized_id='UniProt:P31749', evidence=name,
                       aliases=['AKT1']) for name in ['AKT1 form A', 'AKT1 form B']]
        records = [(CatalogBundle(entities=[row]).entities[0], {'chunk_id': 1}) for row in rows]
        self.assertEqual(len(merge_catalog(records, {}, 1)), 2)

    def test_modification_mechanism_accepts_enzyme_and_rejects_unresolved_group(self):
        source = 'MTOR phosphorylates AKT1 at S473.'
        row = assertion(entity('MTOR', 'Protein'), 'phosphorylates', entity('AKT1', 'Protein'), source)
        row['qualifiers']['site'] = 'S473'
        result = prepare([row], source)
        self.assertFalse(result._claim_rejections)
        self.assertEqual(result.claims[0].predicate, 'PHOSPHORYLATES')
        for role, kind in [('subject', 'ProteinGroup'), ('subject', 'Assay'), ('object', 'Gene')]:
            bad = copy.deepcopy(row)
            bad[role]['type'] = kind
            self.assertTrue(prepare([bad], source)._claim_rejections)

    def test_predicted_sites_keep_predicted_status_and_never_become_experimental(self):
        source = 'The model predicts that MTOR phosphorylates AKT1.'
        row = assertion(entity('MTOR', 'Protein'), 'PHOSPHORYLATES', entity('AKT1', 'Protein'), source)
        row['qualifiers'].update(evidence_type='computational_prediction', knowledge_level='predicted')
        result = prepare([row], source)
        self.assertFalse(result._claim_rejections)
        self.assertEqual(result.claims[0].qualifiers['knowledge_level'], 'predicted')
        del row['qualifiers']['knowledge_level']
        self.assertTrue(prepare([row], source)._claim_rejections)
        row['qualifiers']['evidence_type'] = 'cooccurrence'
        self.assertTrue(prepare([row], source)._claim_rejections)

    def test_abundance_and_ptm_measurements_cannot_be_relabelled_as_activation(self):
        source = 'Treatment affects AKT1.'
        for aspect in ['abundance', 'phosphorylation', 'occupancy']:
            row = assertion(entity('Treatment', 'Intervention'), 'AFFECTS',
                            entity('AKT1', 'Protein', process={'kind': aspect, 'direction': 'increased'}), source)
            self.assertFalse(prepare([row], source)._claim_rejections)
            row['predicate'] = 'ACTIVATES'
            self.assertTrue(prepare([row], source)._claim_rejections)

    def test_modified_molecule_identity_survives_process_splitting(self):
        source = 'Treatment reduced the abundance of AKT1 S473 phosphorylated.'
        row = assertion(entity('Treatment', 'Intervention'), 'AFFECTS',
                        entity('AKT1 S473 phosphorylated', 'Proteoform', process={'kind': 'abundance', 'direction': 'decreased'}), source)
        row['object']['mention'] = 'the abundance of AKT1 S473 phosphorylated'
        result = prepare([row], source)
        self.assertFalse(result._claim_rejections)
        self.assertEqual(result.claims[0].object.text, 'AKT1 S473 phosphorylated')
        self.assertEqual(result.claims[0].object.process.kind, 'abundance')

    def test_ambiguous_peptide_mapping_is_not_a_single_protein_or_complex(self):
        source = 'PEPTIDEK maps to protein group 1.'
        row = assertion(entity('PEPTIDEK', 'Peptide'), 'PEPTIDE_OF', entity('protein group 1', 'ProteinGroup'), source)
        self.assertFalse(prepare([row], source)._claim_rejections)
        for predicate in ['MEMBER_OF', 'PROTEOFORM_OF']:
            row['predicate'] = predicate
            self.assertTrue(prepare([row], source)._claim_rejections)

    def test_bare_residue_labels_require_qualifiers_in_catalog_and_claims(self):
        source = 'AKT1 has site S473.'
        bare = entity('S473', 'ModificationSite', evidence=source)
        with self.assertRaisesRegex(CatalogValidationError, 'parent protein'):
            prepare_catalog(CatalogBundle(entities=[bare]), {'text': source})
        row = assertion(entity('AKT1', 'Protein'), 'HAS_SITE', entity('S473', 'ModificationSite'), source)
        self.assertTrue(prepare([row], source)._claim_rejections)
        # A fabricated parent in text cannot bypass validation when the actual
        # mention only grounds the bare residue label.
        row['object']['text'] = 'AKT1 S473'
        self.assertTrue(prepare([row], source)._claim_rejections)
        source = 'AKT1 has site AKT1 S473.'
        row['object'] = entity('AKT1 S473', 'ModificationSite')
        row['evidence']['sentence'] = source
        self.assertFalse(prepare([row], source)._claim_rejections)

    def test_comparison_keeps_focal_group_metric_site_and_direction(self):
        source = 'AKT1 phosphorylation at S473 was higher in treated than controls.'
        row = assertion(entity('AKT1', 'Protein', process={'kind': 'phosphorylation', 'direction': 'increased'}),
                        'DIFFERS_BETWEEN', entity('treated', 'StudyGroup'), source)
        row['qualifiers'].update(comparison_group='treated', reference_group='controls',
                                 metric='phosphorylation', direction='increased', site='S473')
        result = prepare([row], source)
        self.assertFalse(result._claim_rejections)
        self.assertEqual({p.role for p in result.claims[0].participants}, {'comparison_group', 'reference_group'})
        qualifiers = fact_qualifiers(result.claims[0])
        self.assertEqual(qualifiers['processes'][0], {'role': 'subject', 'kind': 'phosphorylation', 'direction': 'increased'})
        self.assertEqual(qualifiers['site'], 'S473')
        for key in ['reference_group', 'metric', 'direction']:
            bad = copy.deepcopy(row)
            del bad['qualifiers'][key]
            self.assertTrue(prepare([bad], source)._claim_rejections)

    def test_correlation_sign_does_not_create_an_effect_or_binding(self):
        source = 'AKT1 was negatively correlated with inflammation.'
        row = assertion(entity('AKT1', 'Protein'), 'negatively correlated with', entity('inflammation', 'Phenotype'), source)
        row['qualifiers']['evidence_type'] = 'observational'
        result = prepare([row], source)
        self.assertEqual(result.claims[0].predicate, 'CORRELATED_WITH')
        self.assertEqual(result.claims[0].qualifiers['association_direction'], 'negative')
        self.assertIsNone(result.claims[0].object.process)

    def test_repeated_participant_roles_keep_process_targets_and_stable_identity(self):
        source = 'Treatment affects AKT1 and TP53.'
        row = assertion(entity('Treatment', 'Intervention'), 'AFFECTS', entity('AKT1', 'Protein'), source,
                        participants=[{'role': 'target', 'entity': entity(name, 'Protein', process={'kind': aspect})}
                                      for name, aspect in [('AKT1', 'phosphorylation'), ('TP53', 'abundance')]])
        claim, ids = Claim.model_validate(row), {'Treatment': 1, 'AKT1': 2, 'TP53': 3}
        def project(value):
            with patch('app.graph_store.lock_graph_projection'), patch('app.graph_store.upsert_entity',
                       side_effect=lambda cur, identity, **kwargs: ids[identity.canonical_name]):
                return _claim_projection(MagicMock(), value, document_id=1)
        projection = project(claim)
        self.assertEqual({p['entity_id']: p['kind'] for p in projection['qualifiers']['processes']},
                         {2: 'phosphorylation', 3: 'abundance'})
        claim.participants.reverse()
        self.assertEqual(project(claim)['qualifiers_hash'], projection['qualifiers_hash'])
        claim.participants[0].entity.process.kind = 'phosphorylation'
        self.assertNotEqual(project(claim)['qualifiers_hash'], projection['qualifiers_hash'])

    def test_entity_merge_rewires_process_targets_in_relations_and_complete_facts(self):
        qualifiers = {'participants': [['target', 3]], 'processes': [
            {'role': 'target', 'entity_id': 3, 'kind': 'phosphorylation', 'direction': 'increased'}]}
        relation = dict(id=20, subject_entity_id=1, object_entity_id=2, predicate='AFFECTS',
                        qualifiers=copy.deepcopy(qualifiers), negated=False, speculative=False)
        cur = MagicMock()
        cur.fetchall.side_effect = [[{'id': 10, 'document_id': 1}], [relation], [{'role': 'target', 'entity_id': 4}]]
        cur.fetchone.side_effect = [{'id': 21}, dict(predicate='AFFECTS', qualifiers=copy.deepcopy(qualifiers), negated=False, speculative=False)]
        _merge_entities(cur, {'id': 4, 'normalized_id': None}, [{'id': 3, 'normalized_id': None, 'entity_type': 'Protein'}])
        insert = next(c.args[1][3].obj for c in cur.execute.call_args_list if 'INSERT INTO relations' in c.args[0])
        update = next(c.args[1][1].obj for c in cur.execute.call_args_list if 'UPDATE facts SET fact_key=' in c.args[0])
        for projected in [insert, update]:
            self.assertEqual(projected['processes'][0]['entity_id'], 4)
            self.assertEqual(projected['participants'], [('target', 4)])

    def test_registry_prompts_and_schema_share_vocabulary_without_new_transport(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import llm, main
        self.assertEqual(set(get_args(ProcessKind)), set(PROCESS_DEFINITIONS))
        self.assertEqual(main.entity_types()['types'], TYPE_DEFINITIONS)
        self.assertEqual(main.predicate_types()['predicates'], PREDICATES)
        prompt = llm.build_extraction_messages('Results', 'synthetic source')[1]['content']
        for name in PREDICATES:
            self.assertIn(name + ':', prompt)
        with patch.object(llm, 'chat_completion', return_value=json.dumps({'entities': []})) as chat:
            llm.extract_catalog('Results', 'synthetic source')
        self.assertIn('Proteoform', chat.call_args.args[0][1]['content'])
        example = Claim.model_validate(json.loads(llm.COMPARISON_EXAMPLE))
        self.assertFalse(prepare([example], example.evidence.sentence)._claim_rejections)
        response = MagicMock()
        response.choices[0].message.content = '{"claims":[]}'
        response.choices[0].finish_reason = 'stop'
        with patch.object(llm.client.chat.completions, 'create', return_value=response) as create:
            llm.extract_claims('Results', 'synthetic source')
        self.assertEqual(create.call_args.kwargs['max_tokens'], llm.CLAIMS_MAX_OUTPUT_TOKENS)
        self.assertNotIn('tools', create.call_args.kwargs)
        self.assertNotIn('response_format', create.call_args.kwargs)
