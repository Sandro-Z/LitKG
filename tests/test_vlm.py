"""Vision extraction regressions; no running services or model are required."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf

from app import vlm
from app.catalog import merge_catalog
from app.provenance import claim_source_refs
from app.schemas import CatalogEntity, ClaimBundle, parse_model_bundle


SENTENCE = 'Figure 1. Aspirin inhibits COX-1.'


def claim_data():
    return {'subject': {'text': 'Aspirin', 'type': 'Drug', 'mention': 'Aspirin'},
            'predicate': 'INHIBITS', 'object': {'text': 'COX-1', 'type': 'Protein', 'mention': 'COX-1'},
            'evidence': {'sentence': SENTENCE, 'table_or_figure': 'Figure 1'},
            'confidence': 0.9, 'source_kind': 'primary'}


def located():
    return {'kind': 'pdf_page', 'page': 2, 'pdf_sha256': 'a' * 64, 'image_sha256': 'b' * 64,
            'render_version': vlm.RENDER_VERSION, 'dpi': 150, 'max_edge': 2400,
            'width': 1000, 'height': 1400, 'bbox': [0, 0, 500, 700],
            'observation_basis': 'vlm_visual_description'}


def result_payload():
    return {'observations': SENTENCE, 'claims': {'claims': [claim_data()]},
            'repairs': [], 'rejections': [], 'image_source': located(), 'model': 'vision-test'}


def entries():
    return merge_catalog([(CatalogEntity(text=name, type=kind, mention=name, evidence=SENTENCE), {})
                          for name, kind in [('Aspirin', 'Drug'), ('COX-1', 'Protein')]], {}, 1)


def chunk():
    return {'id': 7, 'document_id': 1, 'chunk_index': 2, 'section_title': 'PDF image page 2 (VLM)',
            'text': 'Pending', 'page_start': 2, 'page_end': 2,
            'page_spans': vlm.visual_spans('Pending', located()), 'source_path': '/unused.pdf',
            'abbreviation_map': {}, 'catalog_version': 'v1', 'catalog_status': 'done', 'pipeline_generation': 1}


class VLMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import tasks
        cls.tasks = tasks

    def make_pdf(self, directory):
        path = Path(directory) / 'sample.pdf'
        with pymupdf.open() as pdf:
            first = pdf.new_page(width=400, height=600)
            first.insert_text((30, 50), 'Plain article text only. ' * 8)
            second = pdf.new_page(width=400, height=600)
            second.insert_text((30, 50), SENTENCE)
            second.draw_rect((50, 100, 150, 300), color=(1, 0, 0), fill=(1, 0, 0))
            second.draw_line((50, 350), (300, 350))
            pdf.save(path)
        return path

    def test_disabled_branch_returns_original_text_without_loading_images(self):
        original = [{'text': 'Original paragraph'}]
        with patch.dict(os.environ, {'VLM_ENABLED': 'false'}), patch('app.vlm.image_chunks') as images:
            metadata, chunks = vlm.parse_sources('unused.pdf', lambda _: ({'doi': '10.1234/example'}, original))
        self.assertIs(chunks, original)
        images.assert_not_called()
        self.assertEqual(metadata['doi'], '10.1234/example')

    def test_page_images_include_vectors_and_have_actual_page_locators(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'VLM_ENABLED': 'true'}):
            path = self.make_pdf(directory)
            chunks = vlm.image_chunks(path)
            self.assertEqual([c['page_start'] for c in chunks], [1, 2])
            source = vlm.image_source(chunks[1])
            png, context, info = vlm.render_page(path, source)
            self.assertTrue(png.startswith(b'\x89PNG'))
            self.assertIn('Figure 1', context)
            self.assertEqual(info['image_sha256'], hashlib.sha256(png).hexdigest())
            self.assertLessEqual(max(info['width'], info['height']), 2401)
            candidates = vlm.image_chunks(path, settings=vlm.Settings(page_mode='figures'))
            self.assertEqual([c['page_start'] for c in candidates], [2])

    def test_changed_pdf_is_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'VLM_ENABLED': 'true'}):
            path = self.make_pdf(directory)
            source = vlm.image_source(vlm.image_chunks(path)[0])
            path.write_bytes(path.read_bytes() + b'\nchanged')
            with self.assertRaisesRegex(vlm.VLMError, 'content changed'):
                vlm.render_page(path, source)

    def test_image_discovery_failure_retains_text_and_independent_failed_chunk(self):
        original = {'text': 'Original paragraph'}
        with patch.dict(os.environ, {'VLM_ENABLED': 'true'}), patch('app.vlm.image_chunks', side_effect=ValueError('invalid')):
            _, chunks = vlm.parse_sources('unused.pdf', lambda _: ({}, [original]))
        self.assertIs(chunks[0], original)
        self.assertEqual(vlm.image_source(chunks[1])['discovery_error'], 'ValueError')

    def test_image_only_pdf_has_a_processing_branch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'VLM_ENABLED': 'true'}):
            path = self.make_pdf(directory)
            def empty(_):
                raise RuntimeError('No text sections extracted from PDF')
            _, chunks = vlm.parse_sources(path, empty)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(vlm.image_source(c) for c in chunks))

    def test_other_text_errors_are_not_silently_replaced(self):
        with patch.dict(os.environ, {'VLM_ENABLED': 'true'}):
            with self.assertRaisesRegex(RuntimeError, 'broken reader'):
                vlm.parse_sources('unused.pdf', MagicMock(side_effect=RuntimeError('broken reader')))

    def request(self, response_data, *, status=200):
        response = MagicMock(status_code=status)
        response.json.return_value = response_data
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value = response
        return session

    def test_transport_sends_real_image_to_independent_model_and_checks_cancellation(self):
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'observations': SENTENCE, 'claims': [claim_data()]})}}]}
        session = self.request(response)
        check = MagicMock()
        with patch('app.vlm.render_page', return_value=(b'png-test', 'Figure 1', located())), patch('app.vlm.requests.Session', return_value=session):
            payload = vlm.extract_image('unused.pdf', located(), [], check=check)
        sent = session.post.call_args.kwargs['json']
        self.assertEqual(sent['model'], 'Qwen3.8-Flash-Next-NVFP4')
        self.assertNotIn('response_format', sent)
        self.assertTrue(sent['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertFalse(session.trust_env)
        self.assertNotIn('headers', session.post.call_args.kwargs)
        self.assertGreaterEqual(check.call_count, 3)
        self.assertEqual(payload['observations'], SENTENCE)

    def test_truncated_empty_malformed_and_http_outputs_are_failures(self):
        cases = [
            ({'choices': [{'finish_reason': 'length', 'message': {'content': '{"observations":"", "claims":[]}'}}]}, 200),
            ({'choices': [{'finish_reason': 'stop', 'message': {'content': 'not JSON'}}]}, 200),
            ({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'observations': '', 'claims': [claim_data()]})}}]}, 200),
            ({}, 503),
        ]
        for data, status in cases:
            with self.subTest(status=status, data=data), patch('app.vlm.render_page', return_value=(b'png', '', located())), patch('app.vlm.requests.Session', return_value=self.request(data, status=status)):
                with self.assertRaises(vlm.VLMError):
                    vlm.extract_image('unused.pdf', located(), [])

    def test_preparation_reuses_entity_catalog_and_exports_visual_provenance(self):
        c = chunk()
        bundle = vlm.prepare_image_result(result_payload(), c, entries(), 'v1')
        self.assertEqual(len(bundle.claims), 1, bundle._claim_rejections)
        self.assertFalse(bundle.claims[0].subject.pending_merge)
        self.assertFalse(bundle.claims[0].object.pending_merge)
        evidence = bundle.claims[0].evidence
        self.assertEqual(evidence.modality, 'pdf_image')
        refs = claim_source_refs({'document_id': 1, 'chunk_id': 7, '_source_text': c['text'],
                                  'claim_json': bundle.claims[0].model_dump(mode='json')})
        self.assertEqual(refs[0]['image_source']['page'], 2)
        self.assertEqual(refs[0]['offset']['basis'], 'vlm_visual_description')

    def test_unobserved_candidate_is_rejected_and_marks_partial_coverage(self):
        payload = result_payload()
        payload['observations'] = 'Figure 1. Unrelated labels.'
        bundle = vlm.prepare_image_result(payload, chunk(), entries(), 'v1')
        self.assertEqual(bundle.claims, [])
        self.assertEqual(len(bundle._processing_gaps), 1)

    def test_indexed_observation_is_bound_without_model_copying_evidence(self):
        raw = claim_data()
        raw['observation_index'] = 0
        raw['evidence']['sentence'] = 'An unrelated model paraphrase.'
        text, bundle = vlm.parse_image_output({'observations': [SENTENCE], 'claims': [raw]})
        self.assertEqual(text, SENTENCE)
        self.assertEqual(bundle.claims[0].evidence.sentence, SENTENCE)
        self.assertEqual(bundle._claim_rejections, [])

    def test_invalid_visual_anchor_preserves_valid_sibling_and_reports_gap(self):
        good = claim_data()
        good['observation_index'] = 0
        bad = {**claim_data(), 'observation_index': 99}
        text, parsed = vlm.parse_image_output({'observations': [SENTENCE], 'claims': [good, bad]})
        payload = result_payload()
        payload.update(observations=text, claims=parsed.model_dump(mode='json'), rejections=parsed._claim_rejections)
        result = vlm.prepare_image_result(payload, chunk(), entries(), 'v1')
        self.assertEqual(len(result.claims), 1)
        self.assertEqual(len(result._claim_rejections), 1)
        self.assertEqual(len(result._processing_gaps), 1)

    def test_visual_description_does_not_change_document_catalog_version_inputs(self):
        before = chunk()
        after = copy.deepcopy(before)
        after.update(text=SENTENCE, page_spans=vlm.visual_spans(SENTENCE, located()))
        after['page_spans'][0]['image_source']['width'] = 1234
        self.assertEqual(vlm.chunk_signature(before), vlm.chunk_signature(after))
        text = {'chunk_index': 0, 'section_title': 'Results', 'text': 'Original', 'page_spans': []}
        changed = {**text, 'text': 'Changed'}
        self.assertNotEqual(vlm.chunk_signature(text), vlm.chunk_signature(changed))

    def test_disabling_branch_prevents_saved_image_tasks_from_calling_model(self):
        with patch.dict(os.environ, {'VLM_ENABLED': 'false'}), patch('app.vlm.extract_image') as inference:
            with self.assertRaisesRegex(vlm.VLMError, 'branch disabled'):
                self.tasks.extract_chunk_claims(1, chunk(), entries(), 'v1')
        inference.assert_not_called()

    def test_visual_part_of_direction_contradiction_is_rejected_without_reversing(self):
        sentence = 'Figure 1a shows the bai operon containing the genes baiB and baiF.'
        raw = {'subject': {'text': 'bai operon', 'type': 'Gene', 'mention': 'bai operon'},
               'predicate': 'PART_OF', 'object': {'text': 'baiB', 'type': 'Gene', 'mention': 'baiB'},
               'evidence': {'sentence': sentence}, 'confidence': 0.9}
        payload = result_payload()
        payload.update(observations=sentence, claims={'claims': [raw]})
        result = vlm.prepare_image_result(payload, chunk(), [], 'v1')
        self.assertEqual(result.claims, [])
        self.assertIn('direction contradicts', result._claim_rejections[0]['error'])
        self.assertEqual(result._claim_rejections[0]['claim']['subject']['text'], 'bai operon')
        raw = {**raw, 'subject': raw['object'], 'object': raw['subject']}
        payload['claims'] = {'claims': [raw]}
        result = vlm.prepare_image_result(payload, chunk(), [], 'v1')
        self.assertEqual(len(result.claims), 1, result._claim_rejections)

    def test_text_review_reextract_skips_visual_chunks_and_preserves_source(self):
        from app.review_repair import make_plan
        c = {**chunk(), 'status': 'done'}
        extractor = MagicMock()
        plan = make_plan({'id': 1, 'status': 'done', 'catalog_version': 'v1'}, [c], [], [],
                         mode='reextract', extractor=extractor)
        extractor.assert_not_called()
        self.assertEqual(plan['changes'], [])
        self.assertIn('VLM image task', plan['reviews'][0]['reason'])
        edit = {'chunk_id': 7, 'start': 0, 'end': 7, 'original': 'Pending',
                'replacement': 'Edited', 'basis': 'test', 'page': 2}
        with self.assertRaisesRegex(ValueError, 'not PDF text corrections'):
            make_plan({'id': 1, 'status': 'done'}, [c], [], [], mode='reextract',
                      corrections=[edit], extractor=extractor)

    def test_page_repair_leaves_visual_chunks_and_evidence_untouched(self):
        from app import page_repair
        c = {**chunk(), 'status': 'done'}
        class Connection:
            def execute(self, query, params=None):
                self.query = query
                return self
            def fetchone(self):
                return {'id': 1, 'status': 'done', 'source_path': '/unused.pdf'}
            def fetchall(self):
                if 'FROM chunks' in self.query:
                    return [c]
                if 'FROM claims' in self.query:
                    return [{'id': 8, 'chunk_id': 7, 'evidence_text': SENTENCE,
                             'claim_json': {'evidence': {'sentence': SENTENCE, 'modality': 'pdf_image'}}}]
                return []
            def cursor(self):
                return self
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        @contextmanager
        def connect():
            yield Connection()
        index = MagicMock()
        before = copy.deepcopy(c)
        with patch.object(page_repair, 'get_conn', connect), patch.object(page_repair, 'fallback_pdf_text', return_value=({}, [{'text': 'Paper'}])), patch.object(page_repair.PdfPageIndex, 'from_sections', return_value=index), patch.object(page_repair, 'archive_claims'):
            result = page_repair.repair_document_pages(1)
        index.page_spans.assert_not_called()
        index.locate.assert_not_called()
        self.assertEqual(result['changed_chunk_count'], 0)
        self.assertEqual(result['changed_claim_count'], 0)
        self.assertEqual(c, before)

    def test_models_cannot_forge_image_provenance(self):
        raw = claim_data()
        raw['evidence'].update(modality='pdf_image', image_source=located(), page=999)
        bundle = parse_model_bundle({'claims': [raw]}, ClaimBundle)
        self.assertEqual(bundle.claims[0].evidence.modality, 'text')
        self.assertIsNone(bundle.claims[0].evidence.image_source)
        self.assertIsNone(bundle.claims[0].evidence.page)

    def test_cache_reuses_image_output_after_text_and_render_metadata_change(self):
        rows = {}
        class Cache:
            def execute(self, query, params):
                key = tuple(params[:3])
                if query.startswith('SELECT payload'):
                    self.row = {'payload': copy.deepcopy(rows[key])} if key in rows else None
                elif query.startswith('INSERT INTO processing_cache'):
                    rows[key] = copy.deepcopy(params[3].obj)
                else:
                    raise AssertionError(query)
                return self
            def fetchone(self):
                return self.row
        cache = Cache()
        @contextmanager
        def connect():
            yield cache
        source_chunk = chunk()
        with patch.dict(os.environ, {'VLM_ENABLED': 'true'}), patch.object(self.tasks, 'get_conn', connect), patch.object(self.tasks, 'check_processing'), patch('app.vlm.file_digest', return_value='a' * 64), patch('app.vlm.extract_image', return_value=result_payload()) as inference:
            first = self.tasks.extract_chunk_claims(1, source_chunk, entries(), 'v1')
            second = self.tasks.extract_chunk_claims(1, source_chunk, entries(), 'v1')
            with patch('app.vlm.file_digest', return_value='c' * 64):
                with self.assertRaisesRegex(vlm.VLMError, 'content changed'):
                    self.tasks.extract_chunk_claims(1, source_chunk, entries(), 'v1')
        self.assertEqual(len(first.claims), 1)
        self.assertEqual(len(second.claims), 1)
        inference.assert_called_once()

    def test_visual_chunks_write_through_existing_graph_store_with_correct_model(self):
        c = chunk()
        bundle = vlm.prepare_image_result(result_payload(), c, [], 'v1')
        class Connection:
            def __init__(self):
                self.queries = []
                self.calls = []
            def execute(self, query, params=None):
                self.queries.append(query)
                self.calls.append((query, params))
                return self
            def fetchone(self):
                if 'SELECT ch.*' in self.queries[-1]:
                    return copy.deepcopy(c)
                return {'catalog_version': 'v1', 'catalog_status': 'done', 'pipeline_generation': 1}
            def cursor(self):
                return self
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        connection = Connection()
        @contextmanager
        def connect():
            yield connection
        @contextmanager
        def lock(_):
            yield True
        with patch.object(self.tasks, 'get_conn', connect), patch.object(self.tasks, 'processing_lock', lock), patch.object(self.tasks, 'check_processing'), patch.object(self.tasks, 'load_catalog', return_value=[]), patch.object(self.tasks, 'update_chunk_status'), patch.object(self.tasks, 'extract_chunk_claims', return_value=bundle), patch.object(self.tasks, 'lock_graph_projection'), patch.object(self.tasks, 'archive_claims'), patch.object(self.tasks, 'store_claim', return_value=(101, 201)) as store, patch.object(self.tasks, 'upsert_entity', return_value=1), patch.object(self.tasks, 'add_review'), patch.object(self.tasks, 'refresh_document_status', return_value='done'):
            outcome = self.tasks.extract_chunk.run.__wrapped__(7, 'v1', 1, refresh=False)
        self.assertEqual(outcome['claim_count'], 1)
        self.assertEqual(store.call_args.kwargs['extraction_model'], 'vision-test')
        self.assertEqual(store.call_args.kwargs['claim'].evidence.page, 2)
        self.assertEqual(store.call_args.kwargs['claim'].evidence.modality, 'pdf_image')
        self.assertTrue(any('UPDATE chunks SET text=' in q for q in connection.queries))
        locations = [p[-1].obj['locations'] for q, p in connection.calls if q.startswith('INSERT INTO document_entities')]
        self.assertTrue(locations)
        self.assertTrue(all(location['source_kind'] == 'pdf_image' for group in locations for location in group))


if __name__ == '__main__':
    unittest.main()
