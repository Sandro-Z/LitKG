"""Deterministic matching and candidate isolation; no production data or models."""
import os
import unittest
from unittest.mock import Mock, patch

from app.catalog import merge_catalog, source_contains, source_span, prepare_catalog
from app.claims import prepare_claims, retains_required_context
from app.normalization import entity_identity
from app.schemas import CatalogBundle, CatalogEntity, ClaimBundle, parse_model_bundle
from test_catalog_robustness import MemoryCache


SOURCE = 'Aspirin reduces phosphorylated human AKT1 activity in A549 cells at 5 nM.'


def claim(**extra):
    value = {'subject': {'text': 'Aspirin', 'type': 'Drug'}, 'predicate': 'REDUCES',
        'object': {'text': 'phosphorylated human AKT1', 'type': 'Protein',
                   'mention': 'phosphorylated human AKT1 activity', 'process': {'kind': 'activity'}},
        'participants': [{'role': 'Cell-Line', 'entity': {'text': 'A549', 'type': 'CellLine'}}],
        'qualifiers': {'dose': 5, 'unit': 'nM'}, 'evidence': {'sentence': SOURCE}, 'confidence': .9}
    return {**value, **extra}


def directory():
    return merge_catalog([(CatalogEntity(text=name, type=typ, evidence=name), {})
        for name, typ in [('Aspirin', 'Drug'), ('AKT1', 'Protein'), ('A549', 'CellLine')]], {}, 1)


class ClaimGroundingTests(unittest.TestCase):
    def test_wrong_and_unknown_directory_ids_are_rematched_deterministically(self):
        entries = directory()
        for wrong_id in ['E_unknown', next(e['local_id'] for e in entries if e['text'] == 'AKT1')]:
            data = claim()
            data['subject']['directory_id'] = wrong_id
            bundle = prepare_claims(ClaimBundle(claims=[data]), entries, {'id': 1, 'text': SOURCE}, 'v')
            self.assertEqual(bundle.claims[0].subject.directory_id, next(e['local_id'] for e in entries if e['text'] == 'Aspirin'))
            self.assertTrue(any(r['field'] == 'directory_id' for r in bundle._claim_repairs))

    def test_complete_modified_identity_survives_base_directory_id_and_process_split(self):
        data = claim()
        data['object']['text'] = 'AKT1'
        data['object']['directory_id'] = next(e['local_id'] for e in directory() if e['text'] == 'AKT1')
        bundle = prepare_claims(ClaimBundle(claims=[data]), directory(), {'id': 1, 'text': SOURCE}, 'v')
        entity = bundle.claims[0].object
        self.assertEqual(entity.text, 'phosphorylated human AKT1')
        self.assertTrue(entity.pending_merge)
        self.assertIsNone(entity.directory_id)
        self.assertEqual(entity.process.kind, 'activity')
        self.assertEqual(bundle.claims[0].qualifiers, {'dose': 5, 'unit': 'nM'})
        self.assertEqual(bundle.claims[0].participants[0].role, 'cell_line')

    def test_unicode_layout_roles_and_verified_aliases_are_accepted(self):
        source = 'ASPIRIN reduces IL‑6 in A549 cells. Interleukin-6 (IL-6) was measured.'
        entries = merge_catalog([(CatalogEntity(text='Interleukin-6', type='Protein', aliases=['IL-6'], evidence='IL-6'), {})], {}, 1)
        data = claim(object={'text': 'IL-6', 'type': 'Protein'}, evidence={'sentence': 'aspirin reduces IL-6 in A549 cells.'})
        data['participants'][0]['role'] = ' cellLine '
        value = prepare_claims(ClaimBundle(claims=[data]), entries, {'id': 1, 'text': source}, 'v')
        self.assertEqual(value.claims[0].object.text, 'Interleukin-6')
        self.assertEqual(value.claims[0].participants[0].role, 'cell_line')
        self.assertEqual(value.claims[0].evidence.sentence, source.split('. ')[0]+'.')

    def test_verified_canonical_name_can_recover_its_local_abbreviation(self):
        source = 'Aspirin reduces IL‑6 in A549 cells.'
        entries = merge_catalog([(CatalogEntity(text='Interleukin-6', type='Protein', aliases=['IL-6'], evidence='IL-6'), {})], {}, 1)
        data = claim(object={'text':'Interleukin-6', 'type':'Protein'}, evidence={'sentence':source})
        result = prepare_claims(ClaimBundle(claims=[data]), entries, {'id':1, 'text':source}, 'v')
        self.assertEqual(result.claims[0].object.text, 'Interleukin-6')
        self.assertEqual(result.claims[0].object.mention, 'IL‑6')

    def test_grounded_conflicting_standard_id_remains_a_separate_pending_node(self):
        entries = directory()
        aspirin = next(e for e in entries if e['text']=='Aspirin')
        aspirin.update(normalized_id='CHEBI:1', identifiers=['CHEBI:1'])
        source = SOURCE + ' Aspirin: CHEBI : 2.'
        data = claim()
        data['subject']['normalized_id'] = 'CHEBI:2'
        value = prepare_claims(ClaimBundle(claims=[data]), entries, {'id':1, 'text':source}, 'v')
        self.assertEqual(value.claims[0].subject.normalized_id, 'CHEBI:2')
        self.assertTrue(value.claims[0].subject.pending_merge)
        a = entity_identity('Aspirin', 'Drug', 'CHEBI:2', {}, 1, True)
        b = entity_identity('Aspirin', 'Drug', 'CHEBI:3', {}, 1, True)
        self.assertNotEqual(a.canonical_key, b.canonical_key)

    def test_catalog_alias_requires_equivalence_not_just_cooccurrence(self):
        value = CatalogBundle(entities=[CatalogEntity(text='AKT1', type='Protein', aliases=['EGFR'], evidence='AKT1 and EGFR were measured.')])
        fixed = prepare_catalog(value, {'text':'AKT1 and EGFR were measured.'})
        self.assertEqual(fixed.entities[0].aliases, [])

    def test_id_format_and_soft_hyphen_layout_are_deterministic(self):
        source = 'Phosphory\u00adlated IL ‑ 6 was measured (UniProt : P12345).'
        self.assertTrue(source_contains(source, 'phosphorylated IL-6'))
        value = CatalogBundle(entities=[CatalogEntity(text='IL-6', type='Protein', normalized_id='uniprot:p12345', evidence='IL-6')])
        fixed = prepare_catalog(value, {'text':source})
        self.assertEqual(fixed.entities[0].normalized_id, 'uniprot:p12345')

    def test_malformed_and_ungrounded_candidates_keep_original_indices(self):
        value = parse_model_bundle({'claims':[{'bad':'candidate'}, claim(evidence={'sentence':'Invented sentence.'}), claim()]}, ClaimBundle)
        result = prepare_claims(value, directory(), {'id':1, 'text':SOURCE}, 'v')
        self.assertEqual([r['claim_index'] for r in result._claim_rejections], [0,1])

    def test_source_validation_and_location_share_punctuation_and_word_boundaries(self):
        for text, quote in [('“ﬁrst” café\u0301.', '"first" café\u0301.'), ('A\n\tB.', 'a b.'), ('catalysis', 'cat'), ('A binds B', 'A binds B.')]:
            self.assertEqual(source_contains(text, quote), source_span(text, quote) is not None)
        self.assertFalse(source_contains('catalysis', 'cat'))
        self.assertFalse(source_contains('A binds B', 'A binds B.'))

    def test_bad_optional_evidence_is_removed_but_missing_required_participant_rejects_candidate(self):
        data = claim(additional_evidence=[{'sentence': 'Invented extra sentence.'}, {'broken': True}])
        data['evidence']['page'] = 'not a page'
        value = parse_model_bundle({'claims': [data, claim(participants=[{'role': 'assay', 'entity': {'text': 'Invented assay'}}])]}, ClaimBundle)
        fixed = prepare_claims(value, directory(), {'id': 1, 'text': SOURCE}, 'v')
        self.assertEqual(len(fixed.claims), 1)
        self.assertEqual(fixed.claims[0].additional_evidence, [])
        self.assertEqual(fixed.claims[0].participants[0].entity.text, 'A549')
        self.assertEqual(len(fixed._claim_rejections), 1)
        self.assertTrue(any(r['field'] == 'additional_evidence' for r in fixed._claim_repairs))

    def test_malformed_candidate_does_not_discard_valid_sibling(self):
        value = parse_model_bundle({'claims': [claim(), {'predicate': 'INVENTED'}]}, ClaimBundle)
        fixed = prepare_claims(value, directory(), {'id': 1, 'text': SOURCE}, 'v')
        self.assertEqual(len(fixed.claims), 1)
        self.assertEqual(fixed._claim_rejections[0]['claim_index'], 1)

    def test_unsupported_main_evidence_is_isolated_without_reanchoring(self):
        value = ClaimBundle(claims=[claim(), claim(evidence={'sentence': 'Aspirin cures all diseases.'})])
        fixed = prepare_claims(value, directory(), {'id': 1, 'text': SOURCE}, 'v')
        self.assertEqual(len(fixed.claims), 1)
        self.assertIn('main evidence', fixed._claim_rejections[0]['error'])

    def test_ambiguous_alias_preserves_independent_grounded_identity(self):
        entries = [{'local_id': f'E{i}', 'text': name, 'aliases': ['AR'], 'type': 'Protein',
                    'normalized_id': None, 'ambiguity': 'shared alias'}
                   for i, name in enumerate(['Androgen receptor', 'Aldose reductase'])]
        data = claim(object={'text': 'AR', 'type': 'Protein', 'directory_id': 'E0'}, evidence={'sentence': 'Aspirin reduces AR in A549.'})
        result = prepare_claims(ClaimBundle(claims=[data]), entries, {'id': 1, 'text': data['evidence']['sentence']}, 'v')
        self.assertTrue(result.claims[0].object.pending_merge)
        self.assertEqual(result.claims[0].object.text, 'AR')
        self.assertIsNone(result.claims[0].object.directory_id)

    def test_species_mutation_modification_isoform_and_id_do_not_merge(self):
        names = ['AKT1', 'human AKT1', 'mouse AKT1', 'phosphorylated AKT1', 'AKT1 T790M', 'AKT1 isoform 2']
        identities = [entity_identity(n, 'Protein', 'UniProt:P12345') for n in names]
        self.assertEqual(len({i.canonical_key for i in identities}), len(names))
        records = [(CatalogEntity(text=n, type='Protein', normalized_id='UniProt:P12345', aliases=['AKT1'], evidence=n), {}) for n in names]
        self.assertEqual(len(merge_catalog(records, {}, 1)), len(names))

    def test_catalog_complete_mention_restores_modifiers_and_removes_unsupported_id(self):
        value = CatalogBundle(entities=[CatalogEntity(text='AKT1', mention='phosphorylated human AKT1',
            type='Protein', normalized_id='UniProt:WRONG', evidence=SOURCE)])
        fixed = prepare_catalog(value, {'text': SOURCE})
        self.assertEqual(fixed.entities[0].text, 'phosphorylated human AKT1')
        self.assertIsNone(fixed.entities[0].normalized_id)


class ClaimCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import tasks
        cls.tasks = tasks

    def cached(self, cache, first, correction):
        with patch.object(self.tasks, 'get_conn', side_effect=cache.connect):
            return self.tasks.cached_model(1, 'claims', 'key', first, ClaimBundle, 1,
                validate=lambda b: prepare_claims(b, directory(), {'id': 1, 'text': SOURCE}, 'v'),
                retry_call=correction, max_attempts=3)

    def test_only_failed_candidate_is_sent_for_correction_and_success_cache_is_reused(self):
        invalid = claim(evidence={'sentence': 'Invented sentence.'})
        first = Mock(return_value=ClaimBundle(claims=[claim(), invalid]))
        correction = Mock(return_value=ClaimBundle(claims=[claim()]))
        cache = MemoryCache()
        fixed = self.cached(cache, first, correction)
        self.assertEqual(correction.call_args.args[1], {'claims': [{**ClaimBundle(claims=[invalid]).claims[0].model_dump(mode='json'), 'repair_id':'0'}]})
        self.assertEqual(len(fixed.claims), 2)
        second = self.cached(cache, first, correction)
        first.assert_called_once()
        correction.assert_called_once()
        self.assertEqual(second.claims[0].subject.directory_id, fixed.claims[0].subject.directory_id)
        self.assertEqual(cache.attempts[0]['status'], 'done')

    def test_explicit_exclusion_closes_even_an_all_invalid_response(self):
        first = Mock(return_value=ClaimBundle(claims=[claim(evidence={'sentence': 'Invented sentence.'})]))
        correction = Mock(return_value=parse_model_bundle({'claims': [], 'resolution': {'outcome': 'excluded', 'reason': 'No supporting source sentence'}}, ClaimBundle))
        cache = MemoryCache()
        result = self.cached(cache, first, correction)
        self.assertEqual(result.claims, [])
        self.assertEqual(result._claim_rejections[0]['outcome'], 'excluded')
        self.cached(cache, first, correction)
        correction.assert_called_once()
        self.assertNotIn('_processing_gap', cache.payload)

    def test_unresolved_all_invalid_response_stays_incomplete_and_retry_only_repairs_failed_item(self):
        first = Mock(return_value=ClaimBundle(claims=[claim(evidence={'sentence': 'Invented sentence.'})]))
        unresolved = Mock(return_value=parse_model_bundle({'claims': [], 'resolution': {'outcome': 'unresolved', 'reason': 'Insufficient context'}}, ClaimBundle))
        cache = MemoryCache()
        with self.assertRaisesRegex(ValueError, 'no grounded claims'):
            self.cached(cache, first, unresolved)
        self.assertTrue(cache.payload['_processing_gap'])
        repaired = Mock(return_value=ClaimBundle(claims=[claim()]))
        result = self.cached(cache, first, repaired)
        first.assert_called_once()
        repaired.assert_called_once()
        self.assertEqual(len(result.claims), 1)
        self.assertNotIn('_processing_gap', cache.payload)

    def test_correction_cannot_drop_required_roles_or_condition_fields(self):
        first = Mock(return_value=ClaimBundle(claims=[claim(evidence={'sentence': 'Invented sentence.'})]))
        correction = Mock(return_value=ClaimBundle(claims=[claim(participants=[], qualifiers={})]))
        with self.assertRaisesRegex(ValueError, 'required participants or condition'):
            self.cached(MemoryCache(), first, correction)

    def test_empty_correction_without_resolution_does_not_close_failed_candidate(self):
        cache = MemoryCache()
        with self.assertRaises(ValueError):
            self.cached(cache, Mock(return_value=ClaimBundle(claims=[claim(evidence={'sentence': 'Invented'})])), Mock(return_value=ClaimBundle(claims=[])))
        self.assertTrue(cache.payload['_processing_gap'])

    def test_correction_preserves_repeated_roles_and_nested_conditions(self):
        original = claim(qualifiers={'dose':{'value':5,'unit':'nM'}})
        original['participants'].append({'role':'cell_line', 'entity':{'text':'A549'}})
        reduced = ClaimBundle(claims=[claim(qualifiers={'dose':{'value':5}})]).claims[0]
        self.assertFalse(retains_required_context(original, reduced))
        original['participants'].pop()
        self.assertFalse(retains_required_context(original, reduced))

    def test_resolution_cannot_hide_empty_initial_or_malformed_response(self):
        from app import llm
        for field, method, args in [('claims', llm.extract_claims, ('Results',SOURCE)), ('entities', llm.extract_catalog, ('Results',SOURCE))]:
            raw = '{"'+field+'":[],"resolution":{"outcome":"excluded","reason":"unsupported"}}'
            with patch.object(llm, 'chat_completion', return_value=raw):
                with self.assertRaises(llm.ModelOutputError):
                    method(*args)
                with self.assertRaises(llm.ModelOutputError):
                    method(*args, feedback='invalid JSON', previous_output='{"'+field+'":[')
        with self.assertRaises(ValueError):
            parse_model_bundle({'claims':[], 'resolution':{'outcome':'repaired','reason':'empty'}}, ClaimBundle)

    def test_wrong_correction_repair_id_keeps_the_candidate_unresolved(self):
        repaired = ClaimBundle(claims=[claim()])
        repaired._repair_ids = ['unknown']
        excluded = parse_model_bundle({'claims':[], 'resolution':{
            'outcome':'excluded','reason':'Unsupported','repair_id':'unknown'}}, ClaimBundle)
        for result in [repaired, excluded]:
            with self.subTest(resolution=result._resolution):
                first = Mock(return_value=ClaimBundle(claims=[claim(evidence={'sentence':'Invented sentence.'})]))
                cache = MemoryCache()
                with self.assertRaisesRegex(ValueError, 'repair_id'):
                    self.cached(cache, first, Mock(return_value=result))
                self.assertTrue(cache.payload['_processing_gap'])
