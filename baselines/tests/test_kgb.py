"""Offline tests: python -m unittest discover -s tests -v (no network)."""
import copy
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from kgb.anchor import anchor
from kgb.common import digest, save_json
from kgb.graph import GraphBuilder
from kgb.pipelines import neo4j_builder as nb
from kgb.pipelines import pubtator3 as p3
from kgb.text_prep import build, overlap
from kgb.validate import validate

POLICY = {'scope': None, 'ontology': {
    'node_types': {t: t for t in ('Gene', 'Disease', 'Compound', 'Organism', 'Mutation', 'CellLine', 'Other')},
    'type_parents': {},
    'predicates': {
        'CORRELATED_WITH': {'definition': 'correlation', 'subject_types': ['*'], 'object_types': ['*'],
                            'required_qualifiers': ['association_direction'], 'symmetric': True},
        'ASSOCIATED_WITH': {'definition': 'association', 'subject_types': ['*'], 'object_types': ['*'],
                            'required_qualifiers': [], 'symmetric': True},
        'TREATS': {'definition': 'treats', 'subject_types': ['Compound'], 'object_types': ['Disease'],
                   'required_qualifiers': [], 'symmetric': False}}}}

ABSTRACT = 'Aspirin lowers PTGS2 expression. PTGS2 is associated with colorectal cancer. Aspirin treats colorectal cancer.'


def corpus():
    return [{'id': '7', 'title': 'T', 'external_id': 'biored:111', 'parsing_status': 'partial',
             'blocks': [{'id': '70', 'text': ABSTRACT[:60], 'kind': 'text'},
                        {'id': '71', 'text': ABSTRACT[40:], 'kind': 'text'},
                        {'id': 'original:x:1', 'text': ABSTRACT, 'source_origin': 'litkg_original'}]}]


def pubtator_response(text, bioconcept):
    rows = [f'00000|t|{text}', '00000|a|-NoAbstract-']
    for word, ptype, ident in (('Aspirin', 'Chemical', 'MESH:D001241'), ('PTGS2', 'Gene', '5743'),
                               ('colorectal cancer', 'Disease', 'MESH:D015179')):
        if bioconcept != ptype:
            continue
        start = text.find(word)
        while start >= 0:
            rows.append(f'00000\t{start}\t{start + len(word)}\t{word}\t{ptype}\t{ident}')
            start = text.find(word, start + 1)
    return '\n'.join(rows) + '\n'


EXPORT = {'111': {'pmid': 111, 'relations': [
    {'id': 'R1', 'infons': {'type': 'Negative_Correlation', 'score': '0.9',
                            'role1': {'identifier': 'MESH:D001241', 'type': 'Chemical', 'name': 'Aspirin'},
                            'role2': {'identifier': 'MESH:D015179', 'type': 'Disease', 'name': 'Colorectal Neoplasms'}}},
    {'id': 'R2', 'infons': {'type': 'Cotreatment',
                            'role1': {'identifier': 'MESH:D001241', 'type': 'Chemical', 'name': 'Aspirin'},
                            'role2': {'identifier': '5743', 'type': 'Gene', 'name': 'PTGS2'}}}]}}


class FakeClient:
    def __init__(self):
        self.stats, self.calls = Counter(), 0

    def annotate(self, text, bioconcept):
        self.calls += 1
        return pubtator_response(text, bioconcept)

    def export(self, pmids):
        return {p: EXPORT.get(p, {}) for p in pmids}


CFG = {'max_chars': 3000, 'bioconcepts': ['Gene', 'Chemical', 'Disease'], 'window_chars': 12000, 'window_overlap': 800}


class TextPrepTests(unittest.TestCase):
    def test_overlap_removed_and_offsets_map_back(self):
        dt = build(corpus()[0])
        self.assertEqual(dt.source, 'chunks')
        self.assertEqual(dt.overlaps_removed, [{'block_id': '71', 'chars': 20}])
        self.assertEqual(dt.text, ABSTRACT[:60] + '\n\n' + ABSTRACT[60:])
        start = dt.text.find('Aspirin treats')
        block, s, e = dt.locate(start, start + 14)
        self.assertEqual(block, '71')
        self.assertEqual(corpus()[0]['blocks'][1]['text'][s:e], 'Aspirin treats')
        self.assertIsNone(dt.locate(55, 65))  # crosses the block separator

    def test_overlap_threshold_and_fallback_to_original(self):
        self.assertEqual(overlap('abc short', 'short tail'), 0)  # below MIN_OVERLAP
        doc = {'id': 'x', 'blocks': [{'id': 'o', 'text': 'Original page.', 'source_origin': 'litkg_original'}]}
        self.assertEqual((build(doc).source, build(doc).text), ('original', 'Original page.'))
        visual = {'id': 'v', 'blocks': [{'id': 'i', 'text': 'PDF image page 1.', 'page_spans': [{'image_source': {}}]},
                                        {'id': 'o', 'text': 'Original page.', 'source_origin': 'litkg_original'}]}
        self.assertEqual(build(visual).source, 'original')


class AnchorTests(unittest.TestCase):
    def test_unique_exact_match_prefers_chunk_blocks(self):
        ref, reason = anchor(corpus()[0], 'Aspirin treats colorectal cancer.')
        self.assertIsNone(reason)
        self.assertEqual((ref['block_id'], ref['quote']), ('71', 'Aspirin treats colorectal cancer.'))
        self.assertEqual(corpus()[0]['blocks'][1]['text'][ref['start']:ref['end']], ref['quote'])

    def test_ambiguous_across_overlapping_chunks_uses_hint_or_fails(self):
        quote = 'associated with'
        chunks_only = corpus()[0]
        chunks_only['blocks'] = chunks_only['blocks'][:2]
        self.assertEqual(anchor(chunks_only, quote), (None, 'ambiguous'))
        self.assertEqual(anchor(chunks_only, quote, hint_block='71')[0]['block_id'], '71')
        # Ambiguous among chunk blocks: the unique hit in the original page (next tier) is used.
        self.assertEqual(anchor(corpus()[0], quote)[0]['block_id'], 'original:x:1')

    def test_duplicate_within_block_is_ambiguous(self):
        doc = {'id': 'd', 'blocks': [{'id': 'b', 'text': 'X binds Y. X binds Y.'}]}
        self.assertEqual(anchor(doc, 'X binds Y.'), (None, 'ambiguous'))

    def test_whitespace_difference_stores_verbatim_block_text(self):
        doc = {'id': 'd', 'blocks': [{'id': 'b', 'text': 'Aspirin\nlowers   PTGS2 expression.'}]}
        ref, _ = anchor(doc, 'Aspirin lowers PTGS2 expression.')
        self.assertEqual(ref['quote'], 'Aspirin\nlowers   PTGS2 expression.')
        self.assertEqual(anchor(doc, 'Not in text.'), (None, 'not_found'))


class MappingTests(unittest.TestCase):
    def test_predicate_mapping_keeps_unmapped_originals(self):
        rules = json.loads((Path(__file__).resolve().parents[1] / 'mappings' / 'pubtator3_relations.json').read_text())['rules']
        builder = GraphBuilder('t', 't', corpus(), POLICY, relation_map=rules)
        self.assertEqual(builder.map_predicate('positive_correlate'),
                         ('CORRELATED_WITH', {'association_direction': 'positive'}, True))
        self.assertEqual(builder.map_predicate('Association'), ('ASSOCIATED_WITH', {}, True))
        self.assertEqual(builder.map_predicate('cotreat'), ('cotreat', {}, False))       # TODO rule
        self.assertEqual(builder.map_predicate('treat'), ('TREATS', {}, True))
        self.assertEqual(builder.map_predicate('stimulate'), ('stimulate', {}, False))   # ACTIVATES not in policy
        self.assertEqual(builder.unmapped, Counter({'cotreat': 1, 'stimulate': 1}))

    def test_namespaced_identifiers(self):
        self.assertEqual(p3.xrefs_for('Gene', '5743'), ['NCBIGene:5743'])
        self.assertEqual(p3.xrefs_for('Species', '9606'), ['NCBITaxon:9606'])
        self.assertEqual(p3.xrefs_for('Chemical', 'MESH:D001241'), ['MESH:D001241'])
        self.assertEqual(p3.xrefs_for('Variant', 'tmVar:p|SUB|V|1763|M;RS#:1042522'),
                         ['dbSNP:rs1042522', 'tmVar:p|SUB|V|1763|M;RS#:1042522'])
        self.assertEqual(p3.node_id_for('Gene', '-', 'foo')[1], [])


class PubTatorTests(unittest.TestCase):
    def test_native_run_produces_valid_contract(self):
        documents = corpus()
        builder, meta = p3.run_native(documents, POLICY, CFG, {'7': {'pmid': '111'}}, FakeClient())
        graph = builder.result('PubTator3 · test', dict(variant='pubtator3-native', **meta))
        self.assertEqual(graph['metadata']['corpus_sha256'], digest(documents))
        aspirin = next(n for n in graph['nodes'] if n['id'] == 'p3:MESH:D001241')
        self.assertEqual((aspirin['label'], aspirin['types'], aspirin['xrefs']), ('Aspirin', ['Compound'], ['MESH:D001241']))
        claims = {c['qualifiers']['original_predicate']: c for c in graph['claims']}
        neg = claims['Negative_Correlation']
        self.assertEqual((neg['predicate'], neg['qualifiers']['association_direction'],
                          neg['qualifiers']['evidence_method']), ('CORRELATED_WITH', 'negative', 'pubtator_precomputed_doc_level'))
        self.assertEqual(neg['source_refs'][0]['quote'], 'Aspirin treats colorectal cancer.')
        self.assertEqual(claims['Cotreatment']['predicate'], 'Cotreatment')  # unmapped, kept
        self.assertEqual(graph['metadata']['stats']['unmapped_predicates'], {'Cotreatment': 1})
        self.assertNotIn('negated', neg)  # PubTator does not model negation: field omitted
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'graph.pubtator3-native.json'
            save_json(path, graph)
            self.assertEqual(validate(path, documents, POLICY, ['s3cr3t-value'])[0], [])
            broken = copy.deepcopy(graph)
            broken['documents'][0]['title'] = 'changed'
            broken['claims'][0]['source_refs'][0]['quote'] = 'invented'
            save_json(path, broken)
            errors, _ = validate(path, documents, POLICY)
            self.assertTrue(any('documents differ' in e for e in errors))
            self.assertTrue(any('source_refs' in e for e in errors))
            leaked = copy.deepcopy(graph)
            leaked['name'] = 'contains s3cr3t-value'
            save_json(path, leaked)
            self.assertTrue(any('credential' in e for e in validate(path, documents, POLICY, ['s3cr3t-value'])[0]))

    def test_offsets_must_match_the_submitted_text(self):
        stats = Counter()

        class Shifted(FakeClient):
            def annotate(self, text, bioconcept):
                return f'0|t|{text}\n0\t1\t8\tAspirin\tChemical\tMESH:D001241\n' if bioconcept == 'Chemical' else ''
        mentions = p3.ner_document(build(corpus()[0]), Shifted(), CFG, stats)
        self.assertEqual((mentions, stats['mention_offset_mismatch']), ([], 1))

    def test_llm_variant_validates_relations_and_uses_cache(self):
        class FakeLLM:
            model = 'm'

            def __init__(self):
                self.calls = 0

            def ask(self, system, user):
                self.calls += 1
                data = json.loads(user)
                ids = {e['name']: e['id'] for e in data['ENTITIES']}
                return {'relations': [
                    {'subject': ids['Aspirin'], 'predicate': 'TREATS', 'object': ids['colorectal cancer'],
                     'negated': False, 'speculative': None, 'evidence_quote': 'Aspirin treats colorectal cancer.'},
                    {'subject': ids['PTGS2'], 'predicate': 'TREATS', 'object': ids['colorectal cancer'],
                     'evidence_quote': 'PTGS2 is associated with colorectal cancer.'},          # domain violation
                    {'subject': 'E99', 'predicate': 'TREATS', 'object': ids['PTGS2'], 'evidence_quote': 'x'},
                    {'subject': ids['Aspirin'], 'predicate': 'CURES', 'object': ids['PTGS2'], 'evidence_quote': 'x'},
                    {'subject': ids['PTGS2'], 'predicate': 'ASSOCIATED_WITH', 'object': ids['colorectal cancer'],
                     'evidence_quote': 'PTGS2 is linked to cancer.'}]}, {'prompt_tokens': 10, 'completion_tokens': 5}, 'stop'
        with tempfile.TemporaryDirectory() as cache:
            llm = FakeLLM()
            builder, meta = p3.run_llm(corpus(), POLICY, CFG, FakeClient(), llm, cache)
            self.assertEqual(llm.calls, 1)
            self.assertEqual(len(builder.claims), 1)
            claim = builder.claims[0]
            self.assertEqual((claim['predicate'], claim['negated'], claim['qualifiers']['evidence_method']),
                             ('TREATS', False, 'llm_on_pubtator_entities'))
            self.assertNotIn('speculative', claim)  # null -> omitted
            self.assertEqual(dict(builder.drops), {'domain_range': 1, 'unknown_entity': 1,
                                                   'predicate_not_in_policy': 1, 'quote_not_verbatim': 1})
            self.assertEqual(meta['stats']['llm_usage'], {'prompt_tokens': 10, 'completion_tokens': 5})
            again = FakeLLM()
            _, meta2 = p3.run_llm(corpus(), POLICY, CFG, FakeClient(), again, cache)
            self.assertEqual((again.calls, meta2['stats']['llm_cache_hits']), (0, 1))

    def test_failed_bioconcept_is_counted_and_run_continues(self):
        from kgb.http import HttpError

        class Failing(FakeClient):
            def annotate(self, text, bioconcept):
                if bioconcept == 'Gene':
                    raise HttpError('HTTP 400', 400)
                return super().annotate(text, bioconcept)
        stats = Counter()
        mentions = p3.ner_document(build(corpus()[0]), Failing(), CFG, stats)
        self.assertEqual(stats['freetext_failed_Gene'], 1)
        self.assertEqual({m['type'] for m in mentions}, {'Chemical', 'Disease'})

    def test_pieces_respect_limit(self):
        text = ' '.join(f'Sentence number {i} is here.' for i in range(50))
        spans = p3.pieces(text, 100)
        self.assertTrue(all(e - s <= 100 for s, e in spans))
        self.assertEqual(text[spans[0][0]:spans[0][1]].split()[0], 'Sentence')


class Neo4jBuilderTests(unittest.TestCase):
    def test_schema_expansion_and_limit(self):
        nodes, rels, n = nb.schema(POLICY, 'schema', 1000)
        self.assertIn('Compound,TREATS,Disease', rels)
        self.assertEqual(n, 6 * 6 * 2 + 1)
        self.assertEqual(nb.schema(POLICY, 'no-schema', 10), ('', '', 0))
        with self.assertRaises(nb.BuilderError):
            nb.schema(POLICY, 'schema', 10)

    def test_run_exports_relations_with_cosentence_evidence(self):
        class Store:
            def __init__(self):
                self.queries = []

            def query(self, cypher, **params):
                self.queries.append(cypher)
                if cypher == nb.Q_STATUS:
                    return [{'status': 'Completed'}]
                if cypher == nb.Q_CHUNKS:
                    return [{'id': 'c1', 'text': ABSTRACT, 'position': 1}]
                if cypher == nb.Q_ENTITIES:
                    return [{'chunk': 'c1', 'eid': 'e1', 'labels': ['__Entity__', 'Compound'], 'name': 'Aspirin'},
                            {'chunk': 'c1', 'eid': 'e2', 'labels': ['__Entity__', 'Disease'], 'name': 'colorectal cancer'},
                            {'chunk': 'c1', 'eid': 'e3', 'labels': ['__Entity__', 'Drugish'], 'name': 'Elsewhere'}]
                if cypher == nb.Q_RELATIONS:
                    return [{'a': 'e1', 'type': 'TREATS', 'b': 'e2'}, {'a': 'e1', 'type': 'MENTIONS', 'b': 'e3'},
                            {'a': 'e9', 'type': 'TREATS', 'b': 'e2'}]
                return []

        class Client:
            def __init__(self):
                self.calls = []

            def upload(self, *a):
                self.calls.append(('upload', a[0]))

            def extract(self, *a):
                self.calls.append(('extract', a[0]))

        cfg = {'dedicated_database': True, 'model': 'litkg_compat', 'max_schema_triples': 1000}
        with self.assertRaises(nb.BuilderError):
            nb.run(corpus(), POLICY, dict(cfg, dedicated_database=False), 'sametext', Client(), Store())
        store, client = Store(), Client()
        builder, meta = nb.run(corpus(), POLICY, cfg, 'sametext', client, store, sleep=lambda s: None)
        self.assertEqual([c[0] for c in client.calls], ['upload', 'extract'])
        self.assertTrue(client.calls[0][1].endswith('.txt'))
        treats = next(c for c in builder.claims if c['predicate'] == 'TREATS')
        self.assertEqual(treats['source_refs'][0]['quote'], 'Aspirin treats colorectal cancer.')
        other = next(c for c in builder.claims if c['predicate'] == 'MENTIONS')
        self.assertEqual(other['source_refs'], [])                      # no co-sentence -> unanchored
        self.assertEqual(builder.unmapped, Counter({'MENTIONS': 1}))
        self.assertEqual(builder.nodes['nb:Drugish:Elsewhere']['types'], ['Other'])
        self.assertEqual(meta['stats']['relation_endpoint_outside_document'], 1)
        self.assertEqual(store.queries.count(nb.Q_RESET), 2)
        with tempfile.TemporaryDirectory() as cache:
            first = Client()
            nb.run(corpus(), POLICY, cfg, 'sametext', first, Store(), sleep=lambda s: None, cache_root=cache)
            resumed = Client()
            builder2, meta2 = nb.run(corpus(), POLICY, cfg, 'sametext', resumed, Store(), sleep=lambda s: None,
                                     cache_root=cache)
            self.assertEqual((len(first.calls), resumed.calls), (2, []))
            self.assertEqual(meta2['stats']['documents_from_checkpoint'], 1)
            self.assertEqual(len(builder2.claims), len(builder.claims))


@unittest.skipUnless(os.environ.get('KGB_QC_PATH'), 'set KGB_QC_PATH to a LitKG checkout for the QC import dry run')
class QcDryRunTests(unittest.TestCase):
    def test_qc_normalize_accepts_output(self):
        documents = corpus()
        builder, meta = p3.run_native(documents, POLICY, CFG, {'7': {'pmid': '111'}}, FakeClient())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'graph.pubtator3-native.json'
            save_json(path, builder.result('x', dict(variant='v', **meta)))
            errors, notes = validate(path, documents, POLICY, qc_path=os.environ['KGB_QC_PATH'])
        self.assertEqual(errors, [])
        self.assertTrue(any('QC normalize OK' in n for n in notes))


if __name__ == '__main__':
    unittest.main()
