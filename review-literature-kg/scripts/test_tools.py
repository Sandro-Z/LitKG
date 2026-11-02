#!/usr/bin/env python3
"""Contract, regression and parser checks; not an LLM quality benchmark."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
import kg_review_tools as kg
from ingest_sources import chunks, ingest


class ReviewToolsTests(unittest.TestCase):
    def setUp(self):
        self.request = kg.read_json(kg.ROOT / 'assets/examples/request.json')
        self.evidence = copy.deepcopy(self.request['graph']['claims'][0]['source_refs'][0])

    def draft(self):
        e=copy.deepcopy(self.evidence)
        report={'schema_version':'1.0.0', 'run':{'id':'contract-test','prompt_version':kg.VERSION,
                'model':'synthetic-fixture',**kg.snapshots(self.request),'mode':'all'},
                'summary':'Partial example, only C1 was reviewed.',
                'assessments':[{'claim_id':'C1','verdict':'SUPPORTED','confidence':'high',
                'rationale':'The source explicitly states an association with the same context.',
                'evidence':[e],'searched_blocks':[{'doc_id':'D1','block_id':'b1'}], 'search_notes':'Read complete b1.'}],
                'findings':[], 'omission_candidates':[],
                'document_coverage':[{'doc_id':'D1','block_id':'b1','purpose':'fidelity'}],
                'unreviewed_regions':['All other claims and the independent omission audit remain pending.'],
                'limitations':['This is a contract example, not a complete semantic review.'],'proposals':[]}
        report['metrics']=kg.compute(self.request,report)
        return report

    def test_request_schema(self):
        self.assertEqual(kg.schema_errors(self.request,kg.read_json(kg.ROOT/'assets/request.schema.json')),[])

    def test_unknown_fields_and_bool_offsets_rejected(self):
        self.request['graph']['claims'][0]['extra']='do not silently discard'
        self.request['graph']['claims'][0]['source_refs'][0]['start']=True
        errors=kg.schema_errors(self.request,kg.read_json(kg.ROOT/'assets/request.schema.json'))
        self.assertTrue(any('unknown field' in e for e in errors))
        self.assertTrue(any('expected' in e for e in errors))

    def test_exact_evidence_and_bad_offsets(self):
        self.assertTrue(kg.verify(self.request,self.evidence)['valid'])
        for key,value in [('start',-1),('end',100000),('quote','invented'),('block_id','missing')]:
            e=copy.deepcopy(self.evidence);e[key]=value
            self.assertFalse(kg.verify(self.request,e)['valid'])

    def test_unicode_codepoint_offsets(self):
        self.request['documents'][0]['blocks'][0]['text']='甲🧬乙'
        e={'doc_id':'D1','block_id':'b1','start':1,'end':2,'quote':'🧬','role':'context'}
        self.assertTrue(kg.verify(self.request,e)['valid'])

    def test_duplicate_ids_and_dangling_refs(self):
        self.request['graph']['nodes'].append(copy.deepcopy(self.request['graph']['nodes'][0]))
        self.request['graph']['claims'][0]['subject']='missing'
        self.assertGreaterEqual(sum(f['severity']=='blocker' for f in kg.structure(self.request)['findings']),2)

    def test_no_invented_ontology_constraint(self):
        self.assertFalse(any(f['rule_id']=='ONT-01' for f in kg.structure(self.request)['findings']))
        self.request['policy']['ontology']={'node_types':['Other'],'predicates':[]}
        self.assertTrue(any(f['rule_id']=='ONT-01' for f in kg.structure(self.request)['findings']))

    def test_partial_report_valid_and_incomplete(self):
        r=self.draft()
        self.assertEqual(kg.report_errors(self.request,r),[])
        self.assertEqual(r['metrics']['overall'],'INCOMPLETE')
        self.assertEqual(r['metrics']['strict_support_rate_reviewed'],1.0)
        self.assertLess(r['metrics']['claim_coverage'],1.0)

    def test_false_gate_rejected(self):
        r=self.draft();r['metrics']['overall']='PASS'
        self.assertIn('metrics or gate mismatch',kg.report_errors(self.request,r))

    def test_snapshot_and_evidence_tampering_rejected(self):
        r=self.draft();r['run']['corpus_hash']='wrong';r['assessments'][0]['evidence'][0]['quote']='invented'
        errors=kg.report_errors(self.request,r)
        self.assertTrue(any('snapshot' in e for e in errors))
        self.assertTrue(any('invalid evidence' in e for e in errors))

    def test_conflict_requires_two_sources_and_opposing_roles(self):
        r=self.draft();r['assessments'][0]['verdict']='SOURCE_CONFLICT';r['metrics']=kg.compute(self.request,r)
        self.assertTrue(any('conflict needs' in e for e in kg.report_errors(self.request,r)))

    def test_partial_source_prevents_unsupported(self):
        self.request['documents'][0]['parsing_status']='partial'
        r=self.draft();r['assessments'][0]['verdict']='UNSUPPORTED';r['metrics']=kg.compute(self.request,r)
        self.assertTrue(any('unsupported label' in e for e in kg.report_errors(self.request,r)))

    def test_semantic_error_requires_finding(self):
        r=self.draft();r['assessments'][0]['verdict']='PARTIALLY_SUPPORTED';r['metrics']=kg.compute(self.request,r)
        self.assertTrue(any('no confirmed finding' in e for e in kg.report_errors(self.request,r)))

    def test_invalid_finding_path_rejected(self):
        r=self.draft();f=kg.finding('MOD-01','claim','C1','high','/graph/claims/1','Wrong path for C1')
        r['findings']=[f];r['metrics']=kg.compute(self.request,r)
        self.assertTrue(any('target/path mismatch' in e for e in kg.report_errors(self.request,r)))

    def test_gate_priority_and_no_division_by_zero(self):
        r=self.draft();r['assessments']=[];r['document_coverage']=[]
        self.assertIsNone(kg.compute(self.request,r)['strict_support_rate_reviewed'])
        r['findings']=[kg.finding('REL-01','claim','C1','high','/graph/claims/0','test')]
        self.assertEqual(kg.compute(self.request,r)['overall'],'NEEDS_REVISION')
        r['findings'][0]['severity']='blocker'
        self.assertEqual(kg.compute(self.request,r)['overall'],'FAIL')

    def test_sample_reproducible_and_budgeted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'r.json';p.write_text(json.dumps(self.request))
            args={'request_path':str(p),'mode':'sample','sample_size':2,'seed':'fixed'}
            a=kg.dispatch('plan_review',args);b=kg.dispatch('plan_review',args)
            self.assertEqual(a,b);self.assertEqual(len(a['selected_claim_ids']),2)
            self.assertEqual(len(a['unreviewed_claim_ids']),5)

    def test_all_mode_not_described_as_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'r.json';p.write_text(json.dumps(self.request))
            result=kg.dispatch('plan_review',{'request_path':str(p),'mode':'all'})
            self.assertEqual(len(result['selected_claim_ids']),7)
            self.assertNotIn('pseudorandom sample',result['note'])

    def test_add_node_proposal_can_express_missing_entity(self):
        r=self.draft()
        f=kg.finding('MISS-01','graph','graph','medium',None,'An in-scope entity is missing.')
        r['findings']=[f]
        r['proposals']=[{'operation':'add_node','target_id':'E','before':None,
                         'after':{'id':'E','label':'marker E','type':'Entity','aliases':[]},
                         'finding_ids':[f['id']],'evidence':[copy.deepcopy(self.evidence)],
                         'preconditions':['Confirm E identity against the cited source.']}]
        r['metrics']=kg.compute(self.request,r)
        self.assertEqual(kg.report_errors(self.request,r),[])
        r['proposals'][0]['evidence']=[]
        self.assertTrue(any('require evidence' in e for e in kg.report_errors(self.request,r)))

    def test_misrepresented_not_counted_as_pure_omission(self):
        r=self.draft()
        r['findings']=[kg.finding('MOD-01','claim','C2','high','/graph/claims/1','Association upgraded to cause.')]
        r['omission_candidates']=[{'id':'O','subject':'A','predicate':'associated_with',
            'object':{'kind':'entity','value':'B','datatype':None},
            'qualifiers':copy.deepcopy(self.request['graph']['claims'][0]['qualifiers']),
            'evidence':[copy.deepcopy(self.evidence)],'match_status':'misrepresented',
            'matched_claim_ids':['C2'],'rationale':'An existing counterpart has a relation error.'}]
        r['metrics']=kg.compute(self.request,r)
        self.assertEqual(r['metrics']['omission_missing'],0)
        self.assertEqual(r['metrics']['omission_misrepresented'],1)
        self.assertEqual(r['metrics']['candidate_hit_rate'],0.0)
        self.assertEqual(kg.report_errors(self.request,r),[])

    def test_unknown_scope_not_recall_and_no_confirmed_omission(self):
        self.request['policy']['scope']=None
        r=self.draft();r['findings']=[kg.finding('MISS-01','graph','graph','medium',None,'Missing without scope')]
        r['metrics']=kg.compute(self.request,r)
        self.assertIsNone(r['metrics']['candidate_hit_rate'])
        self.assertTrue(any('without scope' in e for e in kg.report_errors(self.request,r)))

    def test_chunk_and_text_ingestion_preserve_source(self):
        text='甲🧬乙\n'*5000
        parts=list(chunks(text))
        self.assertEqual(''.join(p[2] for p in parts),text)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'source.txt';p.write_text(text)
            out=ingest({'documents':[{'id':'D','file':str(p),'title':'Synthetic'}]})
            d=out['documents'][0]
            self.assertEqual(d['parsing_status'],'complete')
            self.assertEqual(''.join(b['text'] for b in d['blocks']),text)
            self.assertEqual(d['source_sha256'],__import__('hashlib').sha256(p.read_bytes()).hexdigest())

    @unittest.skipUnless(importlib.util.find_spec('pypdf'),'pypdf not installed')
    def test_empty_pdf_does_not_become_complete(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'blank.pdf';writer=PdfWriter();writer.add_blank_page(width=100,height=100)
            with p.open('wb') as f:writer.write(f)
            out=ingest({'documents':[{'id':'P','file':str(p)}]})
            self.assertEqual(out['documents'][0]['parsing_status'],'unreadable')
            self.assertTrue(out['warnings'])


if __name__=='__main__': unittest.main(verbosity=2)
