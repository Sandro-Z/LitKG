"""Review regressions use fixed source quotes and model responses, never live APIs."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from app.claim_export import collect_claims, claims_csv, fetch_claim_page
from app.claim_semantics import result_view
from app.claims import prepare_claims
from app.facts import fact_qualifiers
from app.graph_query import current_claim_clause, fetch_graph
from app.input_quality import correct_chunk, inspect_chunk
from app.normalization import entity_identity
from app.review_repair import make_plan
from app.schemas import Claim, ClaimBundle
from test_graph_query import FakeCursor


def entity(text, type_, process=None):
    result = {'text': text, 'type': type_, 'mention': text}
    if process:
        result['process'] = process
    return result


def candidate(sentence, subject=None, object_=None, predicate='AFFECTS', **extra):
    return {'subject': subject or entity('FMT', 'Intervention'), 'predicate': predicate,
        'object': object_ or entity('CD8 cells', 'CellType', {'kind':'abundance', 'direction':'increased'}),
        'evidence': {'sentence': sentence}, 'confidence': .9, 'source_kind':'primary', 'qualifiers': {'evidence_type':'experimental'}, **extra}


def prepared(value, text=None):
    return prepare_claims(ClaimBundle(claims=[value]), [], {'id': 2, 'text': text or value['evidence']['sentence']}, 'v')


class ReviewSemanticsTests(unittest.TestCase):
    def test_antibody_targets_protein_and_reversed_target_is_quarantined(self):
        sentence = 'The anti-PD-1 antibody targets PD-1 in this experiment.'
        row = candidate(sentence, entity('anti-PD-1 antibody', 'Drug'), entity('PD-1', 'Protein'), 'TARGETS')
        result = prepared(row)
        self.assertFalse(result._claim_rejections)
        self.assertEqual(result.claims[0].semantic_status, 'accepted')
        self.assertEqual((result.claims[0].subject.text, result.claims[0].object.text), ('anti-PD-1 antibody', 'PD-1'))
        row['subject'], row['object'] = row['object'], row['subject']
        self.assertFalse(prepared(row).claims)
        brief = candidate('Aspirin targets COX-1.',entity('Aspirin','Drug'),entity('COX-1','Protein'),'TARGETS')
        self.assertEqual(prepared(brief).claims[0].semantic_status,'accepted')

    def test_temporal_progression_is_pending_and_hidden_from_confirmed_graph(self):
        row = candidate('Disease progression occurred after FMT.', object_=entity('Disease progression', 'ClinicalOutcome'), predicate='CAUSES')
        result = prepared(row)
        self.assertEqual(result.claims[0].semantic_status, 'pending')
        self.assertTrue(any('Temporal' in r for r in result.claims[0].review_reasons))
        self.assertIn("<> 'pending'", current_claim_clause())
        self.assertNotIn("<> 'pending'", current_claim_clause(include_pending=True))
        # An explicitly reported causal conclusion still passes.
        row['evidence']['sentence'] = 'FMT caused Disease progression in the experiment.'
        self.assertEqual(prepared(row).claims[0].semantic_status, 'accepted')

    def test_no_effect_unchanged_and_not_significant_remain_distinct(self):
        for sentence, negated in [('FMT did not affect CD8 cells.', True), ('CD8 cells were unchanged after FMT.', False)]:
            row = candidate(sentence, negated=negated)
            row['object']['process']['direction'] = 'unspecified'
            claim = prepared(row).claims[0]
            self.assertFalse(claim.negated)
            self.assertEqual(claim.object.process.direction, 'unchanged')
            self.assertEqual(result_view(claim.model_dump()), 'no_effect_reported')
            self.assertEqual(fact_qualifiers(claim)['result_status'], 'no_effect_reported')
            self.assertEqual(claim.semantic_status,'accepted')
        row = candidate('FMT did not significantly affect CD8 cells.', negated=True)
        row['object']['process']['direction'] = 'unspecified'
        claim = prepared(row).claims[0]
        self.assertFalse(claim.negated)
        self.assertEqual(claim.result_status, 'not_significant')
        self.assertEqual(claim.object.process.direction, 'unspecified')
        self.assertEqual(result_view({'predicate':'AFFECTS', 'negated':True}), 'unknown')
        self.assertEqual(result_view({'predicate':'AFFECTS'}), 'unknown')  # CSV without direction is not an error.
        row['object']['process']['direction']='unchanged'
        self.assertEqual(prepared(row).claims[0].object.process.direction,'unspecified')
        directional=candidate('FMT did not reduce CD8 cells.',predicate='REDUCES',negated=True)
        directional['object']['process']['direction']='decreased'
        directional_claim=prepared(directional).claims[0]
        self.assertTrue(directional_claim.negated)
        self.assertEqual(directional_claim.object.process.direction,'decreased')
        self.assertEqual(directional_claim.result_status,'unknown')

    def test_joint_intervention_and_unsupported_self_regulation(self):
        sentence = 'FMT combined with anti-PD-1 increased CD8 cells in mice at day 7.'
        row = candidate(sentence, qualifiers={'evidence_type':'experimental', 'time':'day 7'})
        self.assertEqual(prepared(row).claims[0].semantic_status, 'pending')
        row['participants'] = [{'role':'co_intervention', 'entity':entity('anti-PD-1','Drug')},
                               {'role':'host', 'entity':entity('mice','Organism')}]
        claim = prepared(row).claims[0]
        self.assertEqual(claim.semantic_status, 'accepted')
        self.assertEqual(len(claim.participants), 2)
        wrong = copy.deepcopy(row)
        wrong['subject'] = entity('CD8 cells', 'CellType')
        self.assertEqual(prepared(wrong).claims[0].semantic_status, 'pending')
        valid = candidate('IL-2 autoregulates IL-2 expression.', entity('IL-2','Protein'),
            entity('IL-2','Protein', {'kind':'expression','direction':'increased'}))
        self.assertEqual(prepared(valid).claims[0].semantic_status, 'accepted')

    def test_method_self_measurement_and_recruitment_are_rejected_but_real_measurement_passes(self):
        bad = candidate('Flow cytometry measured CD8 cells.', entity('Flow cytometry','Assay'), entity('Flow cytometry','Assay'), 'MEASURED_BY')
        self.assertTrue(prepared(bad)._claim_rejections)
        bad['subject'] = entity('CD8 cells','CellType')
        self.assertFalse(prepared(bad)._claim_rejections)
        recruitment = candidate('Patients were recruited from hospital.', entity('Patients','StudyGroup'), entity('hospital','StudyGroup'), 'SUBCLASS_OF')
        self.assertTrue(prepared(recruitment)._claim_rejections)

    def test_evidence_fragment_cannot_borrow_endpoints_from_rest_of_chunk(self):
        row = candidate('Network analyses established', predicate='ASSOCIATED_WITH', object_=entity('PD-1','Protein'))
        result = prepared(row, 'Network analyses established that FMT and PD-1 were associated.')
        self.assertTrue(result._claim_rejections)
        self.assertFalse(result.claims)

    def test_legacy_defaults_and_document_local_identities(self):
        raw = candidate('FMT increased CD8 cells.', speculative=True)
        raw.pop('source_kind')
        claim = Claim.model_validate(raw)
        self.assertEqual(claim.source_kind, 'unknown')
        self.assertTrue(claim.speculative)
        for name, typ in [('Rs','StudyGroup'), ('sample 1','Biospecimen'), ('OTU1','SequenceFeature')]:
            self.assertNotEqual(entity_identity(name,typ,document_id=1).canonical_key,
                                entity_identity(name,typ,document_id=2).canonical_key)


class ExportRegressionTests(unittest.TestCase):
    def test_more_than_500_exported_once_with_nested_fields_and_stable_cursor(self):
        values = [{'id': i, 'claim_json': {'qualifiers':{'dose':{'value':5,'unit':'μg'}},
            'participants':[{'role':'donor', 'entity':{'text':'D1'}}],
            'object': {'process':{'kind':'abundance','direction':'unchanged'}},
            'evidence':{'sentence':'Exact quote','page':3}}, 'processing_version':'v', 'reviews':[]}
            for i in range(1, 1258)]
        calls = []
        def page(cur, *, limit, after_id, **filters):
            calls.append(after_id)
            return {'claims': [v for v in values if v['id'] > after_id][:limit], 'total':len(values)}
        with patch('app.claim_export.fetch_claim_page', side_effect=page):
            result = collect_claims(Mock(), knowledge_base_id=1)
        self.assertEqual(calls, [0,500,1000,1257])
        self.assertEqual((result['exported_count'],result['total']), (1257,1257))
        self.assertEqual([c['id'] for c in result['claims']], list(range(1,1258)))
        self.assertEqual(json.loads(json.dumps(result))['claims'][0], values[0])
        self.assertIn('μg', claims_csv(result['claims']))

    def test_list_total_remains_available_on_empty_page_and_result_filter_is_parameterized(self):
        cursor = FakeCursor([{'total': 701}, []])
        result = fetch_claim_page(cursor, knowledge_base_id=4, offset=1000, result_status='not_significant')
        self.assertEqual(result['total'],701)
        self.assertFalse(result['claims'])
        self.assertIn('not_significant', cursor.executions[0][1])
        self.assertIn('ORDER BY claim.id', cursor.executions[1][0])

    def test_graph_reports_truncation_and_filters_layer_before_counting(self):
        cursor = FakeCursor([{'total':9}, []])
        graph = fetch_graph(cursor, knowledge_base_id=1, limit=2, include_total=True,
            relation_layer='method_sample', result_status='not_significant')
        self.assertEqual(graph['summary']['matched_relation_count'], 9)
        self.assertTrue(graph['summary']['truncated'])
        self.assertIn('MEASURED_BY', cursor.executions[0][1][2])


class InputAndRepairTests(unittest.TestCase):
    def fixture(self):
        text = 'FMT increased CD8 cells.'
        chunk = {'id':2, 'text':text, 'status':'done', 'section_title':'Results', 'page_start':2, 'page_end':2,
            'page_spans':[{'start':0,'end':len(text),'page_start':2,'page_end':2}]}
        document = {'id':1,'status':'done','catalog_status':'done','catalog_version':'v','pipeline_generation':1}
        claims = [{'id':3,'chunk_id':2,'document_id':1,'claim_json':candidate(text)}]
        return document, [chunk], claims

    def test_article_genotype_and_micro_unit_corrections_are_local_and_traceable(self):
        text = 'Controls: +/+; ratio 1/1. Genotype: 1/1. Dose 5 mg; valid dose 20 mg. Concentration 2 mM. Abstract: neighboring paper.'
        chunk = {'id':2, 'text':text, 'page_start':3, 'page_end':3,
            'page_spans':[{'start':0,'end':len(text),'page_start':3,'page_end':3}]}
        issues = inspect_chunk(chunk)
        self.assertEqual({i['kind'] for i in issues}, {'genotype','micro_unit','article_boundary'})
        edits = []
        for old,new,start in [('1/1','+/+',text.index('1/1',text.index('Genotype'))),
                              ('5 mg','5 μg',text.index('5 mg')), ('2 mM','2 μM',text.index('2 mM')),
                              ('Abstract: neighboring paper.','',text.index('Abstract:'))]:
            edits.append({'chunk_id':2,'start':start,'end':start+len(old),'original':old,'replacement':new,
                'page':3,'basis':'Verified against the specified PDF region'})
        updated = correct_chunk(chunk, edits)
        self.assertEqual(updated['text'], 'Controls: +/+; ratio 1/1. Genotype: +/+. Dose 5 μg; valid dose 20 mg. Concentration 2 μM. ')
        self.assertEqual(chunk['text'], text)
        self.assertEqual(updated['page_spans'][0]['page_start'],3)
        self.assertTrue(all(e['original'] and e['basis'] and e['page'] for e in edits))
        with self.assertRaisesRegex(ValueError,'evidence basis'):
            correct_chunk(chunk,[{**edits[0], 'basis':''}])
        with self.assertRaisesRegex(ValueError,'match original'):
            correct_chunk(chunk,[{**edits[0], 'original':'unmatched'}])

    def test_scoped_repair_defaults_to_preview_and_reproject_never_extracts(self):
        doc,chunks,claims = self.fixture()
        extractor = Mock(side_effect=AssertionError('No model allowed'))
        plan = make_plan(doc,chunks,claims,[],mode='reproject',extractor=extractor)
        self.assertTrue(plan['dry_run'])
        self.assertFalse(plan['diff']['added'])
        self.assertEqual(plan['changes'][0]['operation'],'reproject')
        extractor.assert_not_called()
        model = Mock(return_value=ClaimBundle(claims=[claims[0]['claim_json']]))
        extracted = make_plan(doc,chunks,claims,[],mode='reextract',extractor=model,claim_scope=True)
        self.assertEqual(extracted['changes'][0]['delete_ids'],[3])
        self.assertEqual(model.call_args.kwargs['previous_output']['claims'][0]['repair_id'],'0')
        self.assertIn('before',extracted['diff']['modified'][0])

    def test_failed_extraction_never_deletes_old_records_and_source_edits_block_apply(self):
        doc,chunks,claims = self.fixture()
        model = Mock(return_value=ClaimBundle(claims=[candidate('Invented evidence.')]))
        plan = make_plan(doc,chunks,claims,[],mode='reextract',extractor=model)
        self.assertFalse(plan['changes'])
        self.assertTrue(plan['reviews'])
        edit = {'chunk_id':2,'start':0,'end':3,'original':'FMT','replacement':'FMT','page':2,'basis':'Checked PDF'}
        plan = make_plan(doc,chunks,claims,[],mode='reextract',extractor=model,corrections=[edit])
        self.assertFalse(plan['applicable'])

    def test_legacy_structural_error_is_quarantined_without_guessing_new_roles(self):
        doc,chunks,claims=self.fixture()
        claims[0]['claim_json']=candidate('Flow cytometry measured CD8 cells.',entity('Flow cytometry','Assay'),
            entity('Flow cytometry','Assay'),'MEASURED_BY')
        chunks[0]['text']='Flow cytometry measured CD8 cells.'
        plan=make_plan(doc,chunks,claims,[],mode='repair')
        self.assertEqual(plan['changes'][0]['operation'],'quarantine')
        after=plan['changes'][0]['after']
        self.assertEqual(after['subject'],claims[0]['claim_json']['subject'])
        self.assertEqual(after['evidence'],claims[0]['claim_json']['evidence'])
        self.assertEqual(after['semantic_status'],'pending')


if __name__ == '__main__':
    unittest.main()
