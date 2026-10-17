"""Regression cases for production catalog failures; synthetic source, no live LLM."""
from contextlib import contextmanager
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from app.catalog import CatalogValidationError, prepare_catalog, source_contains, source_span, validate_catalog
from app.schemas import CatalogBundle, CatalogEntity


SOURCE = 'Target one (T1) was measured. Probe Q reduced T1.'


def catalog(name='Target one', **kwargs):
    return CatalogBundle(entities=[CatalogEntity(text=name, type='Protein', evidence=SOURCE, **kwargs)])


class CatalogGroundingTests(unittest.TestCase):
    def test_unsupported_alias_is_removed_without_losing_valid_entity(self):
        original = catalog(aliases=['T1', 'Invented expansion'])
        fixed = prepare_catalog(original, {'text': SOURCE})
        self.assertEqual(len(fixed.entities), 1)
        self.assertEqual(fixed.entities[0].aliases, ['T1'])
        self.assertEqual(original.entities[0].aliases, ['T1', 'Invented expansion'])
        self.assertTrue(any(r['field'] == 'alias' for r in fixed._catalog_repairs))
        validate_catalog(fixed, {'text': SOURCE})

    def test_paraphrased_evidence_is_replaced_by_actual_source_sentence(self):
        original = catalog()
        original.entities[0].evidence = 'Target one is the measured protein.'
        fixed = prepare_catalog(original, {'text': SOURCE})
        self.assertEqual(fixed.entities[0].evidence, 'Target one (T1) was measured.')
        self.assertIn(fixed.entities[0].evidence, SOURCE)
        self.assertEqual(original.entities[0].evidence, 'Target one is the measured protein.')

    def test_evidence_from_wrong_sentence_is_reanchored_to_mention(self):
        value = catalog()
        value.entities[0].evidence = 'Probe Q reduced T1.'
        fixed = prepare_catalog(value, {'text': SOURCE})
        self.assertIn('Target one', fixed.entities[0].evidence)
        self.assertEqual(fixed.entities[0].mention, 'Target one')

    def test_source_spelling_can_replace_an_unsupported_model_mention(self):
        fixed = prepare_catalog(catalog(mention='Invented expanded form'), {'text': SOURCE})
        self.assertEqual(fixed.entities[0].mention, 'Target one')
        self.assertTrue(any(r['field'] == 'mention' for r in fixed._catalog_repairs))

    def test_absent_core_mention_fails_but_invented_optional_identifier_is_removed(self):
        with self.assertRaises(CatalogValidationError):
            prepare_catalog(catalog('Invented protein'), {'text': SOURCE})
        fixed = prepare_catalog(catalog(normalized_id='UniProt:FAKE123'), {'text': SOURCE})
        self.assertIsNone(fixed.entities[0].normalized_id)
        self.assertTrue(any(r['field'] == 'normalized_id' for r in fixed._catalog_repairs))

    def test_identity_differences_and_subwords_are_not_fuzzy_matched(self):
        for source, name in [('AKT1 was measured.', 'AKT2'), ('cis-platin was used.', 'trans-platin'),
                             ('Catalysis was measured.', 'cat'), ('IL6 was measured.', 'IL-6')]:
            with self.subTest(name=name):
                with self.assertRaises(CatalogValidationError):
                    prepare_catalog(catalog(name), {'text': source})

    def test_layout_normalization_preserves_original_source_offsets(self):
        for source, quote in [('Protein\n  A was measured.', 'protein a'),
                              ('The ﬁbrin protein was measured.', 'fibrin'),
                              ('Cafe\u0301 was measured.', 'Café'),
                              ('IL–6 was measured.', 'IL-6')]:
            with self.subTest(source=source):
                span = source_span(source, quote)
                self.assertIsNotNone(span)
                self.assertTrue(source_contains(source[span[0]:span[1]], quote))
        self.assertIsNone(source_span('CATalysis and cat.', 'CATalysisX'))
        self.assertEqual(source_span('CATalysis and CAT.', 'CAT'), (14, 17))

    def test_evidence_never_joins_title_and_body(self):
        sources = ['Target one title', 'Results', 'Probe Q was measured.']
        value = catalog()
        value.entities[0].evidence = '\n'.join(sources)
        fixed = prepare_catalog(value, {'text': '\n'.join(sources), 'sources': sources})
        self.assertEqual(fixed.entities[0].evidence, sources[0])

    def test_unchanged_grounded_output_does_not_create_spurious_reviews(self):
        value = catalog(mention='Target one', aliases=['T1'])
        self.assertEqual(prepare_catalog(value, {'text': SOURCE})._catalog_repairs, [])

    def test_bad_candidates_are_isolated_without_fuzzy_identity_replacement(self):
        source = 'Agencourt AMPure XP beads (Beckman Coulter) and sequenced. PCSK9 was measured.'
        value = CatalogBundle(entities=[
            CatalogEntity(text='Beckman Coulson', type='Other', mention='Beckman Coulson',
                          evidence='Agencourt AMPure XP beads (Beckman Coulson) and sequenced.'),
            CatalogEntity(text='PCSK9', type='Protein', evidence='PCSK9 was measured.'),
            CatalogEntity(text='Agencourt AMPure XP beads', type='Compound', evidence=source,
                          normalized_id='PubChem:999999999'),
        ])
        fixed = prepare_catalog(value, {'text':source}, quarantine=True)
        self.assertEqual([e.text for e in fixed.entities], ['PCSK9', 'Agencourt AMPure XP beads'])
        self.assertEqual([r['entity_index'] for r in fixed._catalog_rejections], [0])
        self.assertEqual(fixed._catalog_rejections[0]['entity']['text'], 'Beckman Coulson')
        self.assertIsNone(fixed.entities[1].normalized_id)
        self.assertTrue(any(r['field'] == 'normalized_id' for r in fixed._catalog_repairs))
        validate_catalog(fixed, {'text':source})
        self.assertEqual(len(value.entities), 3)

    def test_quarantine_never_turns_an_invalid_nonempty_response_into_empty_success(self):
        with self.assertRaisesRegex(CatalogValidationError, 'no grounded catalog entries'):
            prepare_catalog(catalog('Invented protein'), {'text':SOURCE}, quarantine=True)


class MemoryCache:
    """Capture cache and audit transactions, including payloads and invalidation."""
    def __init__(self, payload=None):
        self.payload = payload
        self.attempts = []
        self.deleted = False

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params):
        if sql.startswith('SELECT payload'):
            self.row = None if self.payload is None else {'payload': copy.deepcopy(self.payload)}
        elif sql.startswith('DELETE FROM processing_cache'):
            self.payload = None
            self.deleted = True
        elif sql.startswith('INSERT INTO processing_cache'):
            self.payload = copy.deepcopy(params[3].obj)
        elif sql.startswith('INSERT INTO processing_attempts'):
            self.attempts.append({'status': 'failed' if "'failed'" in sql else 'done',
                                  'output': copy.deepcopy(params[4].obj)})
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self):
        return self.row


class CatalogCacheAndRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import tasks, llm
        cls.tasks, cls.llm = tasks, llm

    def run_cached(self, store, first, correction=None, max_attempts=3):
        with patch.object(self.tasks, 'get_conn', side_effect=store.connect):
            return self.tasks.cached_model(1, 'catalog', 'test-key', first, CatalogBundle, 10,
                validate=lambda value: prepare_catalog(value, {'text': SOURCE}),
                retry_call=correction, max_attempts=max_attempts)

    def test_cleaned_payload_is_returned_cached_and_reused_and_raw_is_preserved(self):
        store = MemoryCache()
        value = catalog(aliases=['T1', 'Invented expansion'])
        value._raw_output = '{"original":"model response"}'
        first = Mock(return_value=value)
        result = self.run_cached(store, first)
        self.assertEqual(result.entities[0].aliases, ['T1'])
        self.assertEqual(store.payload['entities'][0]['aliases'], ['T1'])
        audit = store.attempts[0]['output']
        self.assertEqual(audit['original_parsed']['entities'][0]['aliases'], ['T1', 'Invented expansion'])
        self.assertEqual(audit['raw'], value._raw_output)
        self.assertTrue(audit['repairs'])
        reused = self.run_cached(store, first)
        first.assert_called_once()
        self.assertEqual(reused.entities[0].aliases, ['T1'])

    def test_semantic_correction_receives_error_and_previous_output(self):
        store = MemoryCache()
        correction = Mock(return_value=catalog())
        result = self.run_cached(store, Mock(return_value=catalog('Invented protein')), correction)
        error, previous = correction.call_args.args
        self.assertIn('entity[0]', str(error))
        self.assertEqual(previous['entities'][0]['text'], 'Invented protein')
        self.assertEqual([a['status'] for a in store.attempts], ['failed', 'done'])
        self.assertEqual(result.entities[0].text, 'Target one')

    def test_failed_corrections_are_bounded_and_never_cached(self):
        store = MemoryCache()
        first = Mock(return_value=catalog('Invented protein'))
        correction = Mock(return_value=catalog('Invented protein'))
        with self.assertRaises(CatalogValidationError):
            self.run_cached(store, first, correction)
        first.assert_called_once()
        self.assertEqual(correction.call_count, 2)
        self.assertEqual(len(store.attempts), 3)
        self.assertIsNone(store.payload)

    def test_non_model_errors_are_not_retried(self):
        store, correction = MemoryCache(), Mock()
        with self.assertRaisesRegex(ValueError, 'configuration'):
            self.run_cached(store, Mock(side_effect=ValueError('bad configuration')), correction)
        correction.assert_not_called()
        self.assertEqual(len(store.attempts), 1)

    def test_invalid_cached_output_is_audited_then_regenerated(self):
        store = MemoryCache(catalog('Invented protein').model_dump(mode='json'))
        first = Mock(return_value=catalog())
        self.run_cached(store, first)
        first.assert_called_once()
        self.assertTrue(store.deleted)
        self.assertEqual(store.payload['entities'][0]['text'], 'Target one')
        self.assertEqual([a['status'] for a in store.attempts], ['failed', 'done'])

    def test_malformed_json_is_corrected_with_feedback_and_original_response(self):
        store = MemoryCache()
        raw = json.dumps(catalog().model_dump(mode='json'))
        with patch.object(self.llm, 'chat_completion', side_effect=['{"entities":', raw]) as chat:
            result = self.run_cached(store,
                lambda: self.llm.extract_catalog('Results', SOURCE),
                lambda error, previous: self.llm.extract_catalog('Results', SOURCE,
                    feedback=str(error), previous_output=previous))
        messages = chat.call_args.args[0]
        self.assertEqual(messages[-2]['content'], '{"entities":')
        self.assertIn('未通过服务器原文校验', messages[-1]['content'])
        self.assertEqual(result.entities[0].text, 'Target one')
        self.assertEqual(store.attempts[0]['output']['raw'], '{"entities":')

    def test_empty_correction_cannot_hide_a_failed_response(self):
        store = MemoryCache()
        first_raw = json.dumps(catalog('Invented protein').model_dump(mode='json'))
        with patch.object(self.llm, 'chat_completion', side_effect=[first_raw, '{"entities": []}', '{"entities": []}']):
            with self.assertRaisesRegex(self.llm.ModelOutputError, 'empty directory'):
                self.run_cached(store,
                    lambda: self.llm.extract_catalog('Results', SOURCE),
                    lambda error, previous: self.llm.extract_catalog('Results', SOURCE,
                        feedback=str(error), previous_output=previous))
        self.assertIsNone(store.payload)
        self.assertEqual(len(store.attempts), 3)

    def test_initial_explicit_empty_catalog_is_still_valid(self):
        with patch.object(self.llm, 'chat_completion', return_value='{"entities": []}'):
            self.assertEqual(self.llm.extract_catalog('Methods', 'No named items.').entities, [])


if __name__ == '__main__':
    unittest.main()
