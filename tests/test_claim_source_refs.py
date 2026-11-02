import copy
import csv
import io
import json
import unittest
from unittest.mock import patch

from app.claim_export import fetch_claim_page, claims_csv, collect_facts
from app.provenance import claim_source_refs
from test_graph_query import FakeCursor


class ClaimSourceRefsTests(unittest.TestCase):
    def row(self):
        return {'id':11,'document_id':7,'chunk_id':9,'title':'Paper','doi':'10.1234/example',
                'chunk_page_start':3,'chunk_page_end':5,'section_title':'Results',
                'claim_json':{'evidence':{'sentence':'X increased Y.','page':3,'section':'Results'},
                              'additional_evidence':[{'sentence':'A second measurement.','page':5,'page_end':5,'section':'Discussion'}]}}

    def test_primary_and_additional_have_independent_saved_anchors(self):
        row=self.row();before=copy.deepcopy(row)
        refs=claim_source_refs(row)
        self.assertEqual(len(refs),2)
        self.assertEqual([(r['document_id'],r['chunk_id'],r['page'],r['section']) for r in refs],[(7,9,3,'Results'),(7,9,5,'Discussion')])
        self.assertEqual([r['sentence'] for r in refs],['X increased Y.','A second measurement.'])
        self.assertEqual(row,before)

    def test_missing_source_stays_unknown_and_does_not_borrow_primary_page(self):
        row=self.row();row.pop('chunk_page_start');row.pop('chunk_page_end')
        row['claim_json']['additional_evidence'][0].pop('page')
        row['claim_json']['additional_evidence'][0].pop('page_end')
        refs=claim_source_refs(row)
        self.assertIsNone(refs[1]['page'])
        self.assertNotIn('offset',refs[1])
        self.assertEqual(claim_source_refs({}),[])

    def test_offsets_come_from_saved_text_and_ambiguous_matches_are_omitted(self):
        row=self.row();row['_source_text']='Header. X increased Y. Tail.'
        ref=claim_source_refs(row)[0]
        self.assertEqual(row['_source_text'][ref['offset']['start']:ref['offset']['end']],'X increased Y.')
        row['_source_text']='X increased Y. X increased Y.'
        self.assertNotIn('offset',claim_source_refs(row)[0])

    def test_parser_page_spans_can_anchor_additional_evidence_without_model_offsets(self):
        row=self.row();row['_source_text']='X increased Y. A second measurement.'
        row['_page_spans']=[{'start':0,'end':15,'page_start':3,'page_end':3}, {'start':15,'end':35,'page_start':4,'page_end':5}]
        row['claim_json']['additional_evidence']=[{'sentence':'A second measurement.','start':99999,'end':999999}]
        ref=claim_source_refs(row)[1]
        self.assertEqual((ref['page'],ref['page_end']),(4,5))
        self.assertEqual(ref['offset']['start'],15)

    def test_json_list_and_csv_include_refs_and_retain_old_fields(self):
        row=self.row();row.update(predicate='AFFECTS',reviews=[],_source_text='X increased Y. A second measurement.')
        original=copy.deepcopy(row['claim_json'])
        page=fetch_claim_page(FakeCursor([{'total':1},[row]]),knowledge_base_id=2)
        exported=page['claims'][0]
        self.assertEqual(exported['claim_json'],original)
        self.assertEqual(exported['page'],3)
        self.assertEqual(exported['id'],11)
        self.assertNotIn('_source_text',exported)
        csv_row=next(csv.DictReader(io.StringIO(claims_csv([exported]).lstrip('\ufeff'))))
        self.assertEqual(json.loads(csv_row['source_refs']),exported['source_refs'])
        self.assertEqual(json.loads(csv_row['claim_json']),original)

    def test_facts_export_retains_refs_when_internal_source_text_is_removed(self):
        row=self.row();row.update(predicate='AFFECTS',reviews=[],source_text='X increased Y. A second measurement.')
        pages=[[row],[]]
        with patch('app.retrieval.fetch_facts',side_effect=pages):
            facts=collect_facts(FakeCursor([{'total':1}]),knowledge_base_id=2)
        self.assertEqual(len(facts['facts'][0]['source_refs']),2)
        self.assertNotIn('source_text',facts['facts'][0])

    def test_legacy_evidence_text_only_and_unknown_page(self):
        refs=claim_source_refs({'document_id':1,'chunk_id':2,'evidence_text':'Legacy source.'})
        self.assertEqual(refs[0]['sentence'],'Legacy source.')
        self.assertIsNone(refs[0]['page'])
        self.assertIsNone(refs[0]['page_end'])
