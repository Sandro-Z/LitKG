"""Replay historical source/output failures (archive vocabulary is not the runtime schema); no provider or production DB access."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.catalog import (CatalogValidationError, prepare_catalog, segments,
                         split_catalog_segment, source_contains)
from app.claims import normalize_comparison_groups, prepare_claims, retains_required_context
from app.parser import split_into_chunks
from app.schemas import Claim, ClaimBundle, CatalogBundle, CatalogEntity, Process, Evidence, parse_model_bundle
from test_catalog_capacity import SegmentCache

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/batch22_failures.json').read_text())


def case(review_id):
    value = next(c for c in FIXTURE['claims'] if c['review_id'] == review_id)
    return copy.deepcopy(value['claim']), FIXTURE['sources'][value['source']]


class Batch22RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import llm, tasks
        cls.llm, cls.tasks = llm, tasks

    def test_attempt1304_and1306_escape_only_grounded_evidence_and_keep_raw(self):
        for sample in FIXTURE['quotes']:
            with self.subTest(attempt=sample['attempt_id']):
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(sample['raw'])
                with patch.object(self.llm, 'chat_completion', return_value=sample['raw']) as model:
                    bundle = self.llm.extract_claims('C', sample['source'])
                self.assertEqual(len(bundle.claims), 12)
                self.assertEqual(bundle._raw_output, sample['raw'])
                self.assertEqual(len(bundle._claim_repairs), 4)
                self.assertTrue(any('"homeostatic"' in c.evidence.sentence for c in bundle.claims))
                for repair in bundle._claim_repairs:
                    start, end = repair['source_span']
                    self.assertEqual(sample['source'][start:end], repair['replacement'])
                model.assert_called_once()
                with patch.object(self.llm, 'chat_completion', return_value=sample['raw']):
                    with self.assertRaises(self.llm.ModelOutputError):
                        self.llm.extract_claims('C', 'No supporting original quotation.')

    def test_attempt1261_is_repetition_and_never_a_partial_catalog_success(self):
        sample = FIXTURE['catalog_loop']
        decoder, items, pos = json.JSONDecoder(), [], sample['raw'].find('[') + 1
        while pos < len(sample['raw']):
            pos += len(sample['raw'][pos:]) - len(sample['raw'][pos:].lstrip(' \n\r\t,'))
            try:
                item, pos = decoder.raw_decode(sample['raw'], pos)
            except ValueError:
                break
            items.append(item)
        self.assertEqual(len(items), 41)
        self.assertEqual({i['text'] for i in items}, {'Bifidobacterium'})
        self.assertEqual(max(sum(i == item for i in items) for item in items), 36)
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=sample['raw']), finish_reason='length')])
        with patch.object(self.llm.client.chat.completions, 'create', return_value=response):
            with self.assertRaises(self.llm.ModelOutputTruncatedError):
                self.llm.extract_catalog(sample['section_title'], sample['source'])
        # Assemble the complete items only to test the separate valid-JSON loop
        # guard. Production must never salvage this truncated prefix.
        bundle = CatalogBundle.model_validate({'entities': items})
        model, cache = Mock(return_value=bundle), SegmentCache()
        cache.rows[('catalog', 'unrelated-valid')] = {'entities':[]}
        with patch.object(self.tasks, 'get_conn', side_effect=cache.connect):
            with self.assertRaisesRegex(CatalogValidationError, 'Repetitive catalog'):
                self.tasks.cached_model(122, 'catalog', 'loop', model, CatalogBundle, 1555,
                    validate=lambda b:prepare_catalog(b, {'text':sample['source']}, quarantine=True),
                    retry_call=model, max_attempts=3)
        self.assertEqual(model.call_count, 3)
        self.assertNotIn(('catalog', 'loop'), cache.rows)
        self.assertIn(('catalog', 'unrelated-valid'), cache.rows)

    def test_all_logged_entity_shaped_group_labels_normalize_without_guessing(self):
        cases = [c for c in FIXTURE['claims'] if c['error'] == 'DIFFERS_BETWEEN requires qualifiers.comparison_group']
        self.assertEqual(len(cases), 59)
        self.assertEqual(sum(isinstance(c['claim']['qualifiers'].get('comparison_group'), dict) for c in cases), 56)
        normalized = 0
        for value in cases:
            parsed = parse_model_bundle({'claims': [value['claim']]}, ClaimBundle)
            if not parsed.claims:
                # Diversity belongs to the archived microbiome contract, not
                # proteomics. Reject it explicitly without weakening Process.
                self.assertTrue(parsed._claim_rejections)
                self.assertIn('process.kind', parsed._claim_rejections[0]['error'])
                continue
            claim = parsed.claims[0]
            original = claim.model_copy(deep=True)
            try:
                normalize_comparison_groups(claim, [])
            except ValueError:
                continue
            if isinstance(original.qualifiers.get('comparison_group'), dict):
                expected = original.qualifiers['comparison_group']
                self.assertEqual(claim.qualifiers['comparison_group'], expected.get('mention') or expected['text'])
                normalized += 1
            result = prepare_claims(ClaimBundle(claims=[claim]), [],
                {'id':1, 'text':FIXTURE['sources'][value['source']]}, 'test')
            # The archive's remaining collapsed/mismatched/ungrounded groups
            # are actual failures, not 56 facts made valid by format conversion.
            self.assertFalse(result.claims)
            self.assertTrue(result._claim_rejections)
        self.assertGreater(normalized, 50)

    def test_group_generation_example_and_validation_agree_on_real_source(self):
        row, source = case(18729)
        # A reference transcription of the explicitly stated Rs-vs-NRs finding,
        # not an automatic repair of the archived contradictory group labels.
        row['qualifiers']['comparison_group'] = 'Rs'
        row['qualifiers']['reference_group'] = 'NRs'
        row['participants'][0]['entity'].update(text='Rs', mention='Rs')
        row['participants'][1]['entity'].update(text='NRs', mention='NRs')
        result = prepare_claims(ClaimBundle(claims=[row]), [], {'id':1,'text':source}, 'test')
        self.assertEqual(len(result.claims), 1)
        reference = result.claims[0]
        for mutation in ('collapse', 'mismatch', 'absent', 'direction'):
            broken = reference.model_copy(deep=True)
            if mutation == 'collapse':
                broken.participants[0].entity = broken.participants[1].entity.model_copy(deep=True)
                broken.qualifiers['comparison_group'] = 'NRs'
            elif mutation == 'mismatch':
                broken.qualifiers['comparison_group'] = 'NRs'
            elif mutation == 'absent':
                broken.participants[1].entity.mention = 'unreported group'
            else:
                broken.subject.process = Process(kind='abundance',direction='increased')
            rejected = prepare_claims(ClaimBundle(claims=[broken]), [], {'id':1,'text':source}, 'test')
            self.assertFalse(rejected.claims, mutation)
        example = Claim.model_validate(json.loads(self.llm.COMPARISON_EXAMPLE))
        self.assertTrue(all(isinstance(example.qualifiers[k], str) for k in ('comparison_group','reference_group')))

    def test_missing_rs_evidence_requires_original_context_not_dropped_conditions(self):
        row, source = case(18652)
        original = Claim.model_validate(row)
        result = prepare_claims(ClaimBundle(claims=[original]), [], {'id':1,'text':source}, 'test')
        self.assertFalse(result.claims)
        context = ('Although Rs and NRs exhibited similar composition of serum cytokines and chemokines, '
            'Luminex multiparameter proteomics profiling showed a prominent shift in levels of circulating '
            'cytokines and chemokines in Rs, whereas NRs had little or no change [Fig. 4A, principal components analysis (PCA)].')
        self.assertIn(context, source)
        corrected = original.model_copy(deep=True)
        corrected.additional_evidence = [Evidence(sentence=context)]
        self.assertTrue(retains_required_context(row, corrected))
        grounded = prepare_claims(ClaimBundle(claims=[corrected]), [], {'id':1,'text':source}, 'test')
        self.assertEqual(len(grounded.claims), 1)
        self.assertEqual(grounded.claims[0].evidence.sentence, row['evidence']['sentence'])
        removed = corrected.model_copy(deep=True)
        removed.participants = []
        self.assertFalse(retains_required_context(row, removed))
        different = corrected.model_copy(deep=True)
        different.subject.text = different.subject.mention = 'IL-5'
        self.assertFalse(retains_required_context(row, different))

    def test_correction_cannot_replace_assertion_direction_groups_or_condition_values(self):
        row, _ = case(18729)
        original = Claim.model_validate(row)
        for field in ('subject','object','predicate','negated','direction','dose','participant'):
            before = copy.deepcopy(row)
            after = original.model_copy(deep=True)
            if field in ('subject','object'):
                getattr(after, field).text = getattr(after, field).mention = 'different entity'
            elif field == 'predicate': after.predicate = 'ASSOCIATED_WITH'
            elif field == 'negated': after.negated = True
            elif field == 'direction': after.qualifiers['direction'] = 'increased'
            elif field == 'dose':
                before['qualifiers']['dose'] = {'value':5, 'unit':'mg'}
                after.qualifiers['dose'] = {'value':10, 'unit':'mg'}
            else: after.participants[0].entity.text = after.participants[0].entity.mention = 'different group'
            self.assertFalse(retains_required_context(before, after), field)

    def test_cached_correction_cannot_substitute_another_fact_from_the_same_segment(self):
        invalid, source = case(18729)
        valid = copy.deepcopy(invalid)
        valid['qualifiers'].update(comparison_group='Rs', reference_group='NRs')
        valid['participants'][0]['entity'].update(text='Rs', mention='Rs')
        alternate = copy.deepcopy(valid)
        alternate['subject'].update(text='CD4 + Tregs', mention='CD4 + Tregs')
        initial = ClaimBundle(claims=[valid, invalid])
        correction = Mock(return_value=ClaimBundle(claims=[alternate]))
        cache = SegmentCache()
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect):
            bundle = self.tasks.cached_model(117,'claims','same-assertion',Mock(return_value=initial),ClaimBundle,1567,
                validate=lambda b:prepare_claims(b,[],{'id':1567,'text':source},'test'),
                retry_call=correction,max_attempts=2)
        self.assertEqual(len(bundle.claims),1)
        self.assertEqual(len(bundle._claim_rejections),1)
        self.assertIn('original assertion',bundle._claim_rejections[0]['error'])
        correction.assert_called_once()

    def test_chunk1536_and_recursive_segments_keep_word_boundaries_offsets_and_coverage(self):
        sample = FIXTURE['boundary']
        self.assertEqual(sample['text'][-800:], sample['next_text'][:800])
        source = sample['text'] + sample['next_text'][800:]
        self.assertTrue(sample['text'].endswith('efficac'))
        self.assertIn('immunotherapy efficacy', source)
        pieces = list(segments([{'id':1536,'text':source}], 3000))
        for parent in list(pieces):
            pieces.extend(split_catalog_segment(parent, min_chars=500))
        covered = set()
        for part in pieces:
            start,end = part['start'],part['end']
            self.assertEqual(source[start:end], part['text'])
            self.assertFalse(start and source[start-1].isalnum() and source[start].isalnum())
            self.assertFalse(end < len(source) and source[end-1].isalnum() and source[end].isalnum())
            covered.update(range(start,end))
        self.assertEqual(len(covered), len(source))
        shift = len(sample['text']) - 800
        spans = [*sample['page_spans'], *[{**s,'start':s['start']+shift,'end':s['end']+shift}
                                        for s in sample['next_page_spans']]]
        chunks = split_into_chunks([{'section_title':'Body','text':source,'page_spans':spans}])
        coverage = set()
        for chunk in chunks:
            self.assertIn(chunk['text'], source)
            start = source.index(chunk['text'])
            end = start + len(chunk['text'])
            expected = {(max(s['start'],start)-start,min(s['end'],end)-start,s['page_start'],s['page_end'])
                        for s in spans if s['start'] < end and s['end'] > start}
            self.assertEqual({(s['start'],s['end'],s['page_start'],s['page_end']) for s in chunk['page_spans']},expected)
            coverage.update(range(start,end))
            self.assertFalse(chunk['text'].endswith('efficac'))
        self.assertTrue(all(i in coverage for i,c in enumerate(source) if not c.isspace()))
        # The archived invented fragment "effic" is still rejected against the
        # old segment; neither string repair nor new segmentation changes it.
        row, old_source = case(17380)
        result = prepare_claims(ClaimBundle(claims=[row]), [], {'id':1,'text':old_source}, 'test')
        self.assertFalse(result.claims)

    def test_boundary_fix_reuses_existing_initial_and_recursive_cache_plans(self):
        source = FIXTURE['boundary']['text']
        chunk = {'id':1536,'section_title':'Body','text':source,'start':0,'end':len(source)}
        cache = SegmentCache()
        old = list(segments([chunk], 3000, legacy=True))
        for stage,keys in [('catalog',lambda s:self.tasks._catalog_segment_keys('Study',s,0,500)),
                           ('claims',lambda s:self.tasks._claim_segment_keys(s,'test',500))]:
            key,_ = keys(old[0])
            cache.rows[(stage,key)] = {'entities':[]} if stage == 'catalog' else {'claims':[]}
            with patch.object(self.tasks,'get_conn',side_effect=cache.connect):
                selected = list(self.tasks._cached_segments(120,[chunk],3000,stage,keys))
            self.assertEqual([(s['start'],s['end']) for s in selected], [(s['start'],s['end']) for s in old])

        # A pre-patch v1 split tree must retain its original overlapping cuts.
        # Recomputing children with the new cuts would miss every cached leaf.
        key,split_key = self.tasks._catalog_segment_keys('Study',chunk,0,500)
        cache.rows[('catalog_split',split_key)] = {'strategy':'overlapping-halves-v1'}
        children = split_catalog_segment(chunk,500,legacy=True)
        for child in children:
            child_key,_ = self.tasks._catalog_segment_keys('Study',child,0,500)
            entities = [dict(text='PD-1',type='Protein',mention='PD-1',evidence='PD-1')] if 'PD-1' in child['text'] else []
            cache.rows[('catalog',child_key)] = {'entities':entities}
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect), \
             patch.object(self.tasks,'extract_catalog',side_effect=AssertionError('Must reuse successful leaves')) as model:
            leaves = list(self.tasks.catalog_segment_bundles(120,'Study',chunk,0,min_chars=500))
        self.assertEqual([s['text'] for s,_ in leaves],[s['text'] for s in children])
        self.assertTrue(any(e.text == 'PD-1' for _,bundle in leaves for e in bundle.entities))
        model.assert_not_called()
