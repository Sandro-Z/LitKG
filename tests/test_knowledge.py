import copy
import os
import unittest
from unittest.mock import patch

from app.catalog import merge_catalog, segments, resolve_bundle, validate_catalog, source_contains
from app.entity_types import TYPE_DEFINITIONS
from app.facts import fact_qualifiers
from app.normalization import extract_abbreviations, normalize_entity_type
from app.schemas import CatalogBundle, CatalogEntity, ClaimBundle


def bundle(sentence='Probe Q reduces target secretion.'):
    return ClaimBundle.model_validate({'claims': [{
        'subject': {'text': 'Probe Q', 'type': 'Drug'}, 'predicate': 'REDUCES',
        'object': {'text': 'target', 'type': 'Protein', 'mention': 'target secretion',
                   'process': {'kind': 'secretion', 'direction': 'decreased'}},
        'evidence': {'sentence': sentence}, 'confidence': .9,
    }]})


class KnowledgeUnitTests(unittest.TestCase):
    def test_segmentation_covers_entire_long_document_and_tail(self):
        text = 'x' * 49001 + ' Tail entity.'
        parts = list(segments([{'id': 1, 'text': text}], max_chars=4000))
        coverage = [''] * len(text)
        for p in parts:
            coverage[p['start']:p['end']] = p['text']
        self.assertEqual(''.join(coverage), text)
        self.assertTrue(parts[-1]['text'].endswith('Tail entity.'))
        self.assertEqual(parts[-1]['end'], len(text))

    def test_full_document_abbreviation_merges_across_chunks(self):
        definitions = extract_abbreviations('Interleukin-6 (IL-6) was studied.')
        records = [(CatalogEntity(text=name, type='Protein', evidence=name), {'chunk_id': i})
                   for i, name in enumerate(['Interleukin-6', 'IL-6'])]
        entries = merge_catalog(records, definitions, 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]['locations']), 2)
        self.assertEqual(entries[0]['local_id'], merge_catalog(list(reversed(records)), definitions, 1)[0]['local_id'])

    def test_ambiguous_abbreviation_is_not_a_unique_identity(self):
        definitions = extract_abbreviations('Androgen receptor (AR) and aldose reductase (AR) were studied.')
        records = [(CatalogEntity(text=n, type='Protein', evidence=n, aliases=['AR']), {'chunk_id': i})
                   for i, n in enumerate(['Androgen receptor', 'aldose reductase'])]
        entries = merge_catalog(records, definitions, 1)
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e['ambiguity'] for e in entries))
        value = ClaimBundle.model_validate({'claims': [{'subject': {'text':'AR','type':'Protein','directory_id':entries[0]['local_id']},
            'predicate':'BINDS_TO','object':{'text':'X','type':'Compound'},'evidence':{'sentence':'AR binds X.'},'confidence':.8}]})
        resolve_bundle(value, entries, {'id':2,'text':'AR binds X.'}, 'v1')
        self.assertEqual(value.claims[0].subject.text, 'AR')
        self.assertIsNone(value.claims[0].subject.directory_id)
        self.assertTrue(value.claims[0].review_reasons)

    def test_explicit_aliases_merge_across_chunks_but_conflicting_ids_do_not(self):
        records=[(CatalogEntity(text='Aspirin',type='Drug',aliases=['acetylsalicylic acid'],evidence='Aspirin'),{'chunk_id':1}),
                 (CatalogEntity(text='acetylsalicylic acid',type='Compound',evidence='acetylsalicylic acid'),{'chunk_id':2})]
        entries=merge_catalog(records,{},1)
        self.assertEqual(len(entries),1)
        self.assertEqual(len(entries[0]['locations']),2)
        records[0][0].normalized_id='CHEBI:1'
        records[1][0].normalized_id='CHEBI:2'
        self.assertEqual(len(merge_catalog(records,{},1)),2)

    def test_type_registry_covers_real_gaps_and_retains_other(self):
        for name in ['CellType','Tissue','AnatomicalStructure','RNA','MolecularComplex','BiologicalProcess','Phenotype']:
            self.assertIn(name, TYPE_DEFINITIONS)
            self.assertEqual(normalize_entity_type(name), name)
        self.assertEqual(normalize_entity_type('invented_type'), 'Other')

    def test_process_and_conditions_are_preserved_without_stripping_identity(self):
        value = bundle()
        value.claims[0].qualifiers = {'dose':5,'unit':'nM','time':'24 h'}
        entries = merge_catalog([(CatalogEntity(text='target',type='Protein',evidence='target'), {})], {}, 1)
        omissions = resolve_bundle(value, entries, {'id':1,'text':'Probe Q reduces target secretion.'}, 'v')
        self.assertEqual(value.claims[0].object.text, 'target')
        self.assertEqual(value.claims[0].object.mention, 'target secretion')
        self.assertEqual(fact_qualifiers(value.claims[0])['processes'][0]['kind'], 'secretion')
        self.assertEqual(fact_qualifiers(value.claims[0])['dose'], 5)
        self.assertEqual(len(omissions), 1)  # Directory remains open to new entities.
        for kind in ['expression','activity']:
            value.claims[0].object.process.kind = kind
            self.assertEqual(fact_qualifiers(value.claims[0])['processes'][0]['kind'], kind)
        for name in ['EGFR T790M','phosphorylated AKT1','isoform 2','cis-platin']:
            entity = CatalogEntity(text=name,type='Protein',evidence=name)
            self.assertEqual(merge_catalog([(entity,{})],{},1)[0]['text'], name)

    def test_catalog_cannot_supply_relationship_evidence_or_invent_ids(self):
        value = bundle()
        with self.assertRaisesRegex(ValueError, 'evidence'):
            resolve_bundle(value, [], {'id':1,'text':'Unrelated paragraph.'}, 'v')
        with self.assertRaisesRegex(ValueError, 'standard ID'):
            validate_catalog(CatalogBundle(entities=[CatalogEntity(text='target',type='Protein',normalized_id='UniProt:P12345',evidence='target')]), {'text':'target'})
        self.assertFalse(source_contains('catalysis', 'cat'))

    def test_invalid_model_output_is_not_successful_empty_extraction(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import llm
        for raw in ['', '{}', '{"claims":', '{"wrong": []}']:
            with patch.object(llm, 'chat_completion', return_value=raw):
                with self.assertRaises(llm.ModelOutputError):
                    llm.extract_claims('Results', 'source')
        with patch.object(llm,'chat_completion',return_value='{"claims": []}'):
            self.assertEqual(llm.extract_claims('Results','source').claims, [])

    def test_answer_rejects_unknown_and_missing_citations(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ,TEST_ENV):
            from app import llm
        from app.retrieval import answer_question
        result = {'citations':[{'id':'1:0','sentence':'Exact source.'}], 'facts':[], 'warnings':[]}
        for raw in ['{"insufficient_evidence":false,"statements":[{"text":"invented", "citation_ids":["99:0"]}]}',
                    '{"insufficient_evidence":false,"statements":[{"text":"invented", "citation_ids":[]}]}']:
            with patch.object(llm,'chat_completion',return_value=raw):
                answer = answer_question(copy.deepcopy(result),'question')
            self.assertEqual(answer['status'],'evidence_only')
            self.assertNotIn('invented',answer['answer'])
            self.assertTrue(answer['warnings'])
        self.assertEqual(answer_question({'citations':[]},'question')['status'], 'insufficient_evidence')
