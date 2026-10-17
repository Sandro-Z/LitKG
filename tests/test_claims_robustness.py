"""Production failure classes reproduced with synthetic text and controlled LLMs."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from app.catalog import merge_catalog, relevant_entries, resolve_bundle, prepare_catalog
from app.claims import prepare_claims, merge_correction
from app.schemas import CatalogEntity, ClaimBundle
from test_catalog_capacity import SegmentCache

SOURCE = 'Probe Q reduces IL-6. Probe Q binds AKT1.'


def claim(obj='IL-6', **extra):
    return {'subject':{'text':'Probe Q','type':'Drug','mention':'Probe Q'}, 'predicate':'REDUCES',
            'object':{'text':obj,'type':'Protein','mention':obj},
            'evidence':{'sentence':'Probe Q reduces IL-6.'}, 'confidence':.9, **extra}


class ClaimsRobustnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import llm, tasks
        cls.llm, cls.tasks = llm, tasks

    def parse(self, claims):
        with patch.object(self.llm,'chat_completion',return_value=json.dumps({'claims':claims})):
            return self.llm.extract_claims('Results', SOURCE)

    def test_schema_invalid_item_does_not_discard_valid_sibling_and_reports_field_path(self):
        result = self.parse([claim(), {'subject':{'text':'Probe Q'}}])
        self.assertEqual(len(result.claims),1)
        self.assertEqual(len(result._claim_rejections),1)
        self.assertIn('predicate: missing',result._claim_rejections[0]['error'])
        self.assertIn('confidence: missing',result._claim_rejections[0]['error'])

    def test_schema_invalid_catalog_feedback_has_loc_and_type_without_input(self):
        with patch.object(self.llm,'chat_completion',return_value='{"entities":[{"text":55,"evidence":"source"}]}'):
            with self.assertRaisesRegex(self.llm.ModelOutputError, 'entities.0.text: string_type'):
                self.llm.extract_catalog('Results','source')

    def test_missing_catalog_evidence_is_locally_anchored_and_audited(self):
        raw = json.dumps({'entities':[{'text':'Probe Q','mention':'Probe Q','type':'Drug'}]})
        with patch.object(self.llm,'chat_completion',return_value=raw):
            result = prepare_catalog(self.llm.extract_catalog('Results',SOURCE), {'text':SOURCE}, quarantine=True)
        self.assertEqual(result.entities[0].evidence,'Probe Q')
        self.assertTrue(any(r['original'] is None for r in result._catalog_repairs))
        with patch.object(self.llm,'chat_completion',return_value=raw.replace('Probe Q','Invented protein')):
            with self.assertRaises(ValueError):
                prepare_catalog(self.llm.extract_catalog('Results',SOURCE), {'text':SOURCE}, quarantine=True)

    def test_mention_and_evidence_failures_are_isolated_without_rewriting_source(self):
        rows = [claim(), claim('Imaginary protein'), claim(evidence={'sentence':'Unstated relation.'})]
        original = self.parse(rows)
        result = prepare_claims(original, [], {'id':1,'text':SOURCE}, 'v')
        self.assertEqual(len(result.claims),1)
        self.assertEqual(len(result._claim_rejections),2)
        self.assertIn('mention',result._claim_rejections[0]['error'])
        self.assertIn('evidence',result._claim_rejections[1]['error'])
        self.assertEqual(len(original.claims),3)

    def test_directory_prompt_contains_no_cross_chunk_quotes_or_locations(self):
        entries = merge_catalog([(CatalogEntity(text='IL-6',type='Protein',aliases=['Interleukin-6'],evidence='IL-6'),
                                  {'evidence':'A quote from another paragraph.'*1000})],{},1)
        compact = relevant_entries(entries,SOURCE)
        self.assertEqual(len(compact),1)
        self.assertEqual(compact[0]['aliases'],['IL-6'])
        self.assertNotIn('locations',compact[0])
        self.assertLess(len(json.dumps(compact)),500)

    def test_contextual_mention_can_use_canonical_identity_but_not_erase_variants(self):
        entry = merge_catalog([(CatalogEntity(text='IL-6',type='Protein',evidence='IL-6'),{})],{},1)[0]
        for mention, allowed in [('the serum levels of IL-6',True),('phosphorylated IL-6',False),
                                 ('murine IL-6',False),('IL-6 mutant',False),('IL-6 T790M',False),('IL-60',False)]:
            row = claim(evidence={'sentence':f'Probe Q reduces {mention}.'})
            row['object'].update(directory_id=entry['local_id'],mention=mention)
            value = ClaimBundle.model_validate({'claims':[row]})
            chunk = {'id':1,'text':row['evidence']['sentence']}
            if allowed:
                resolve_bundle(value,[entry],chunk,'v')
                self.assertEqual(value.claims[0].object.mention,mention)
            else:
                with self.assertRaises(ValueError):
                    resolve_bundle(value,[entry],chunk,'v')

    def test_wrong_explicit_directory_id_is_not_silently_rebound(self):
        entries = merge_catalog([(CatalogEntity(text=n,type='Protein',evidence=n),{}) for n in ['IL-6','AKT1']],{},1)
        row = claim()
        row['object']['directory_id'] = next(e['local_id'] for e in entries if e['text']=='AKT1')
        with self.assertRaisesRegex(ValueError,'Directory ID'):
            resolve_bundle(ClaimBundle.model_validate({'claims':[row]}),entries,{'id':1,'text':SOURCE},'v')

    def test_missing_directory_id_cannot_bypass_source_modifier_check(self):
        entries = merge_catalog([(CatalogEntity(text='IL-6',type='Protein',evidence='IL-6'),{})],{},1)
        row = claim(evidence={'sentence':'Probe Q reduces murine IL-6.'})
        row['object']['mention'] = 'murine IL-6'
        with self.assertRaisesRegex(ValueError,'modifiers'):
            resolve_bundle(ClaimBundle.model_validate({'claims':[row]}),entries,{'id':1,'text':row['evidence']['sentence']},'v')

    def test_typed_alias_normalization_does_not_relax_source_grounding(self):
        entries = merge_catalog([(CatalogEntity(text='IL-6',type='Protein',evidence='IL-6'),{})],{},1)
        row = claim('IL6',evidence={'sentence':'Probe Q reduces IL6.'})
        row['object']['directory_id'] = entries[0]['local_id']
        result = ClaimBundle.model_validate({'claims':[row]})
        resolve_bundle(result,entries,{'id':1,'text':row['evidence']['sentence']},'v')
        self.assertEqual(result.claims[0].object.text,'IL-6')
        row['evidence']['sentence'] = 'Probe Q reduces IL-6.'
        with self.assertRaisesRegex(ValueError,'mention'):
            resolve_bundle(ClaimBundle.model_validate({'claims':[row]}),entries,{'id':1,'text':'Probe Q reduces IL-6.'},'v')

    def test_correction_cannot_hide_omitted_duplicated_or_unlabelled_claims(self):
        original = ClaimBundle(claims=[])
        original._claim_rejections = [{'error':'bad','original':claim()} for _ in range(2)]
        for ids, remaining in [([None],2),(['0','0'],2),(['0'],1),(['0','1'],0),(['9'],2)]:
            correction = ClaimBundle.model_validate({'claims':[claim() for _ in ids]})
            correction._repair_ids = ids
            result = merge_correction(original,correction)
            self.assertEqual(len(result._claim_rejections),remaining)

    def test_truncated_parent_splits_and_successful_tree_is_reused(self):
        cache = SegmentCache()
        segment = {'id':7,'section_title':'Results','text':SOURCE*5,'start':0,'end':len(SOURCE)*5}
        calls = []
        def model(section_title,chunk_text,directory,**kwargs):
            calls.append(chunk_text)
            if len(chunk_text)>80:
                raise self.llm.ModelOutputTruncatedError('capacity','{"claims":[')
            return ClaimBundle(claims=[])
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect), patch.object(self.tasks,'extract_claims',side_effect=model), patch.dict(os.environ,{'CLAIMS_MIN_SEGMENT_CHARS':'30'}):
            first = list(self.tasks.claim_segment_bundles(1,segment,[],'v'))
            before = list(calls)
            repeated = list(self.tasks.claim_segment_bundles(1,segment,[],'v'))
        self.assertEqual(before,calls)
        self.assertEqual(len(first),len(repeated))
        self.assertGreater(len(first),1)
        self.assertEqual(calls.count(segment['text']),1)
        self.assertTrue(any(stage=='claims_split' for stage,key in cache.rows))

    def test_minimum_truncation_remains_failure_even_when_json_is_parseable(self):
        cache = SegmentCache()
        segment = {'id':7,'section_title':'Results','text':SOURCE,'start':0,'end':len(SOURCE)}
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect), patch.object(self.tasks,'extract_claims',side_effect=self.llm.ModelOutputTruncatedError('capacity','{"claims":[]}')):
            leaves = list(self.tasks.claim_segment_bundles(1,segment,[],'v'))
        self.assertEqual(len(leaves),1)
        self.assertFalse(leaves[0][1].claims)
        self.assertEqual(leaves[0][1]._claim_rejections[0]['kind'],'capacity')

    def test_bounded_correction_preserves_valid_siblings_and_partial_cache_is_retryable(self):
        cache = SegmentCache()
        segment = {'id':7,'section_title':'Results','text':SOURCE,'start':0,'end':len(SOURCE)}
        bad = ClaimBundle.model_validate({'claims':[claim(),claim('Imaginary protein')]})
        calls = []
        def model(**kwargs):
            calls.append(kwargs)
            if kwargs.get('feedback'):
                result = ClaimBundle.model_validate({'claims':[claim()]})
                result._repair_ids = ['0']
                return result
            return bad.model_copy(deep=True)
        with patch.object(self.tasks,'get_conn',side_effect=cache.connect), patch.object(self.tasks,'extract_claims',side_effect=model):
            result = list(self.tasks.claim_segment_bundles(1,segment,[],'v'))[0][1]
            self.assertEqual(len(result.claims),2)
            self.assertFalse(result._claim_rejections)
            # A manual retry must revisit the partial original, not silently
            # upgrade an earlier quarantine to a complete empty extraction.
            repeated = list(self.tasks.claim_segment_bundles(1,segment,[],'v'))[0][1]
        self.assertEqual(len(repeated.claims),2)
        self.assertEqual(len([call for call in calls if not call.get('feedback')]),2)


if __name__ == '__main__':
    unittest.main()
