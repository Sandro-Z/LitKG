"""Counterexamples from the supplied review, with explicit fixture assumptions."""
import json
from pathlib import Path
import unittest

from app.evaluation import evaluate_semantic_cases
from app.input_quality import inspect_document, correct_chunk
from app.schemas import Claim
from test_review_regressions import candidate, entity, prepared


class ReviewEvidenceTests(unittest.TestCase):
    def test_fixed_review_evidence_gate_leaves_missing_backend_fields_unassessed(self):
        cases=json.loads((Path(__file__).parent/'fixtures/fmt_review_semantics.json').read_text())
        report=evaluate_semantic_cases(cases)
        self.assertEqual(report['failed_count'],0,[r for r in report['cases'] if not r['passed']])
        self.assertEqual(report['passed_count'],20)
        self.assertEqual(report['unassessed_count'],4)
        self.assertNotIn('precision',report)
        self.assertNotIn('recall',report)
        self.assertTrue(all(c['provenance'].get('source_anchors') for c in report['cases']))

    def test_negative_and_positive_arms_are_not_collapsed_across_a_contrast(self):
        quote='Stool composition failed to influence natural tumor growth, but increased anti-PD-1 efficacy after blockade.'
        negative=candidate(quote,entity('Stool composition','Microbiome'),
            entity('natural tumor growth','Phenotype',{'kind':'growth','direction':'unspecified'}))
        positive=candidate(quote,entity('Stool composition','Microbiome'),
            entity('anti-PD-1 efficacy','Phenotype',{'kind':'activity','direction':'increased'}))
        self.assertEqual(prepared(negative).claims[0].result_status,'no_effect_reported')
        self.assertEqual(prepared(positive).claims[0].result_status,'observed_change')

    def test_distinct_sentences_do_not_share_the_first_result_polarity(self):
        quote='FMT did not affect CD8 cells. Aspirin increased IL-6 activity.'
        positive=candidate(quote,entity('Aspirin','Drug'),entity('IL-6','Protein',{'kind':'activity','direction':'increased'}))
        negative=candidate(quote)
        self.assertEqual(prepared(positive).claims[0].result_status,'observed_change')
        self.assertEqual(prepared(negative).claims[0].result_status,'no_effect_reported')

    def test_explicit_semantic_uncertainty_is_preserved_for_review(self):
        claim=candidate('FMT increased CD8 cells.',review_reasons=['semantic: joint treatment attribution is uncertain'])
        self.assertEqual(prepared(claim).claims[0].semantic_status,'pending')

    def test_real_article_doi_conflict_and_ob_slash_one_are_located_without_guessing_corrections(self):
        texts=['Parthenogenesis in Komodo dragons. doi:10.1038/nature4441021a Human gut microbes associated with obesity. lean ob/1 mice.',
               'Human gut microbes associated with obesity. doi:10.1038/nature4441022a Rubber bands reduce the cost of carrying loads.']
        chunks=[{'id':i+1,'text':t,'page_start':i+1,'page_end':i+1,
                 'page_spans':[{'start':0,'end':len(t),'page_start':i+1,'page_end':i+1}]} for i,t in enumerate(texts)]
        issues=inspect_document(chunks)
        boundaries=[i for i in issues if i['kind']=='article_boundary']
        self.assertEqual(len(boundaries),2)
        self.assertEqual({i['page'] for i in boundaries},{1,2})
        genotype=next(i for i in issues if i['kind']=='genotype')
        self.assertEqual(genotype['original'],'ob/1')
        self.assertIsNone(genotype['replacement'])
        edit={**genotype,'replacement':'ob/+','basis':'Review D06 physical p1/p4 identifies ob/+; this is an explicit fixture locator'}
        updated=correct_chunk(chunks[0],[edit])
        self.assertIn('lean ob/+ mice',updated['text'])
        self.assertIn('lean ob/1 mice',chunks[0]['text'])
        # A single DOI or an ordinary reference is not an automatic article cut.
        self.assertFalse([i for i in inspect_document(chunks[:1]) if i['kind']=='article_boundary'])

    def test_confirmed_micro_unit_values_require_exact_reviewed_edits(self):
        text='GF: 500 mg; CONVR: 100 mg; cells: 100 mM; valid dietary dose: 20 mg.'
        chunk={'id':1,'text':text,'page_start':23,'page_end':23,'page_spans':[]}
        edits=[]
        for old,new in [('500 mg','500 μg'),('100 mg','100 μg'),('100 mM','100 μM')]:
            start=text.index(old)
            edits.append({'start':start,'end':start+len(old),'original':old,'replacement':new,'page':23,
                'basis':'D15 review notes: PDF p8 Fig3 and p23 Methods visual correction; fixture offsets only'})
        fixed=correct_chunk(chunk,edits)
        self.assertEqual(fixed['text'],'GF: 500 μg; CONVR: 100 μg; cells: 100 μM; valid dietary dose: 20 mg.')
        self.assertEqual(chunk['text'],text)


if __name__=='__main__':
    unittest.main()
