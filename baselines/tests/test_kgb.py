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
    def test_schema_expansion_and_compact_fallback(self):
        nodes, rels, n, mode, instructions = nb.schema(POLICY, 'schema', 1000)
        self.assertIn('Compound,TREATS,Disease', rels)
        self.assertEqual((n, mode, instructions), (6 * 6 * 2 + 1, 'triples', ''))
        self.assertEqual(nb.schema(POLICY, 'no-schema', 10), ('', '', 0, 'none', ''))
        nodes, rels, n, mode, instructions = nb.schema(POLICY, 'schema', 10)
        self.assertEqual((rels, n, mode), ('', 73, 'nodes+instructions'))
        self.assertEqual(nodes.split(','), ['Gene', 'Disease', 'Compound', 'Organism', 'Mutation', 'CellLine'])
        self.assertIn('TREATS [Compound -> Disease]: treats', instructions)
        self.assertIn('CORRELATED_WITH [* -> *]', instructions)
        self.assertNotIn('{', instructions)   # the builder rewrites braces

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
                self.calls.append(('extract', a[0], a))

        cfg = {'dedicated_database': True, 'model': 'litkg_compat', 'max_schema_triples': 1000}
        compact = Client()
        builder_c, meta_c = nb.run(corpus(), POLICY, dict(cfg, max_schema_triples=10), 'sametext', compact, Store(),
                                   sleep=lambda s: None)
        self.assertEqual(meta_c['schema_mode'], 'nodes+instructions')
        self.assertIn('TREATS [Compound -> Disease]', compact.calls[1][2][4])
        self.assertEqual(meta_c['stats'].get('domain_range_violations', 0), 0)
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
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'any-name.pdf'
            pdf.write_bytes(b'%PDF-1.4 test')
            docs = corpus()
            docs[0]['source_sha256'] = 'abc'
            uploaded = Client()
            uploaded.upload = lambda filename, data, model: uploaded.calls.append(('upload', filename, data))
            nb.run(docs, POLICY, cfg, 'pdfnative', uploaded, Store(), sleep=lambda s: None, pdf_index={'abc': str(pdf)})
            self.assertEqual(uploaded.calls[0][2], b'%PDF-1.4 test')
            self.assertTrue(uploaded.calls[0][1].endswith('.pdf'))
        with tempfile.TemporaryDirectory() as cache:
            first = Client()
            nb.run(corpus(), POLICY, cfg, 'sametext', first, Store(), sleep=lambda s: None, cache_root=cache)
            resumed = Client()
            builder2, meta2 = nb.run(corpus(), POLICY, cfg, 'sametext', resumed, Store(), sleep=lambda s: None,
                                     cache_root=cache)
            self.assertEqual((len(first.calls), resumed.calls), (2, []))
            self.assertEqual(meta2['stats']['documents_from_checkpoint'], 1)
            self.assertEqual(len(builder2.claims), len(builder.claims))


class Neo4jBuilderFailureTests(unittest.TestCase):
    """One failing PDF must not end the run; a streak of failures must (systematic problem)."""

    class Store:
        def __init__(self, failing=()):
            self.failing, self.current = set(failing), None

        def query(self, cypher, **params):
            if cypher == nb.Q_STATUS:
                return [{'status': 'Failed' if params['f'] in self.failing else 'Completed'}]
            if cypher == nb.Q_CHUNKS:
                return [{'id': 'c1', 'text': ABSTRACT, 'position': 1}]
            return []

    class Client:
        def __init__(self, raising=()):
            self.raising, self.calls = set(raising), []

        def upload(self, filename, data, model):
            self.calls.append(('upload', filename))

        def extract(self, filename, *a):
            self.calls.append(('extract', filename))
            if filename in self.raising:
                raise nb.BuilderError(f'extract failed for {filename}: Failed To Process File')

    CFG = {'dedicated_database': True, 'model': 'litkg_compat', 'max_schema_triples': 1000}

    def docs(self, n):
        out = []
        for i in range(n):
            doc = corpus()[0]
            doc['id'] = f'd{i}'
            out.append(doc)
        return out

    def test_failed_document_is_retried_listed_and_skipped(self):
        docs = self.docs(3)
        bad = nb.safe_filename('d1') + '.txt'
        client, lines = self.Client(raising={bad}), []
        builder, meta = nb.run(docs, POLICY, self.CFG, 'sametext', client, self.Store(), sleep=lambda s: None,
                               log=lines.append)
        self.assertEqual([c for c in client.calls if c[1] == bad].count(('extract', bad)), 2)   # 2 attempts
        self.assertEqual([f['id'] for f in meta['failed_documents']], ['d1'])
        self.assertIn('Failed To Process File', meta['failed_documents'][0]['error'])
        self.assertEqual(meta['stats']['documents_failed'], 1)
        self.assertEqual((meta['stats']['document_status_Completed'], meta['stats']['chunks']), (2, 2))
        self.assertTrue(any('[2/3]' in line and 'attempt 2/2 failed' in line for line in lines))
        self.assertTrue(any(line.startswith('[3/3]') and 'done' in line for line in lines))
        # a document whose Neo4j status ends as Failed counts the same way
        _, meta = nb.run(self.docs(2), POLICY, dict(self.CFG, extract_attempts=1), 'sametext', self.Client(),
                         self.Store(failing={nb.safe_filename('d0') + '.txt'}), sleep=lambda s: None, log=lines.append)
        self.assertEqual([f['id'] for f in meta['failed_documents']], ['d0'])

    def test_failure_streak_stops_the_run(self):
        names = {nb.safe_filename(f'd{i}') + '.txt' for i in range(5)}
        client = self.Client(raising=names)
        with self.assertRaisesRegex(nb.BuilderError, '3 documents in a row failed'):
            nb.run(self.docs(5), POLICY, dict(self.CFG, extract_attempts=1), 'sametext', client, self.Store(),
                   sleep=lambda s: None, log=lambda m: None)
        self.assertEqual(len([c for c in client.calls if c[0] == 'extract']), 3)

    def test_extract_error_points_to_the_builder_log(self):
        class Response:
            def json(self):
                return {'status': 'Failed', 'message': 'Failed To Process File:x.pdf or LLM Unable To Parse Content '}

        class FakeHttp:
            def request(self, *a, **k):
                return Response()

        client = nb.BuilderClient({'backend_url': 'http://b', 'uri': 'bolt://x', 'user': 'neo4j', 'database': 'neo4j',
                                   'token_chunk_size': 200, 'chunk_overlap': 20, 'chunks_to_combine': 1}, 'pw',
                                  http=FakeHttp())
        with self.assertRaisesRegex(nb.BuilderError, r'Parse Content \(the cause is in the builder log: .*docker-logs\)'):
            client.extract('x.pdf', 'litkg_compat', '', '')

    def test_extract_sends_the_fields_the_backend_reads(self):
        sent = {}

        class Response:
            def json(self):
                return {'status': 'Success', 'data': {}}

        class FakeHttp:
            def request(self, method, url, data=None, **k):
                sent.update(data)
                return Response()

        client = nb.BuilderClient({'backend_url': 'http://b', 'uri': 'bolt://x', 'user': 'neo4j', 'database': 'neo4j',
                                   'token_chunk_size': 200, 'chunk_overlap': 20, 'chunks_to_combine': 1}, 'pw',
                                  http=FakeHttp())
        client.extract('x.pdf', 'litkg_compat', 'Gene,Disease', '', 'Use ONLY ...')
        # backend/src/main.py processing_source() passes these to load_embedding_model(); None fails the extraction
        self.assertEqual((sent['embedding_provider'], sent['embedding_model']), ('sentence-transformer', 'all-MiniLM-L6-v2'))
        for field in ('model', 'source_type', 'file_name', 'allowedNodes', 'allowedRelationship', 'token_chunk_size',
                      'chunk_overlap', 'chunks_to_combine', 'additional_instructions', 'uri', 'userName', 'password',
                      'database'):
            self.assertIn(field, sent)

    def test_end_to_end_smoke_test(self):
        class Store(self.Store):
            queries = []

            def query(self, cypher, **params):
                self.queries.append(cypher)
                return super().query(cypher, **params)

        client, store = self.Client(), Store()
        seconds, chunks, entities, relations = nb.smoke(POLICY, self.CFG, client, store, sleep=lambda s: None)
        self.assertEqual(chunks, 1)
        self.assertEqual(client.calls, [('upload', nb.SMOKE_FILE), ('extract', nb.SMOKE_FILE)])
        self.assertEqual((store.queries[0], store.queries[-1]), (nb.Q_RESET, nb.Q_RESET))   # leaves the database empty
        Store.queries = []
        with self.assertRaisesRegex(nb.BuilderError, 'Failed To Process File'):
            nb.smoke(POLICY, self.CFG, self.Client(raising={nb.SMOKE_FILE}), Store(), sleep=lambda s: None)
        self.assertEqual(Store.queries[-1], nb.Q_RESET)
        with self.assertRaisesRegex(nb.BuilderError, 'end-to-end test'):
            nb.smoke(POLICY, self.CFG, self.Client(), Store(failing={nb.SMOKE_FILE}), sleep=lambda s: None)

    def test_smoke_pdf_is_readable(self):
        import importlib.util
        if importlib.util.find_spec('pypdf') is None:
            self.skipTest('pypdf not installed')
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(nb.minimal_pdf(nb.SMOKE_TEXT + ' (x) a\\b')), strict=True)
        self.assertEqual(' '.join(reader.pages[0].extract_text().split()), nb.SMOKE_TEXT + ' (x) a\\b')

    def test_builder_env_matches_compose_file(self):
        compose = (Path(__file__).resolve().parents[1] / 'deploy' / 'docker-compose.builder.yml').read_text()
        for key, value in nb.BUILDER_ENV.items():
            self.assertIn(f'{key}: "{value}"', compose)


class CorpusTests(unittest.TestCase):
    def test_pdfs_become_documents_with_litkg_policy(self):
        import importlib.util
        if importlib.util.find_spec('pypdf') is None:
            self.skipTest('pypdf not installed')
        import shutil
        import sys
        repo = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo / 'tests'))
        from pdf_fixtures import text_pdf
        from kgb.corpus import build as build_corpus
        with tempfile.TemporaryDirectory() as directory:
            pdfs = Path(directory) / 'pdfs'
            (pdfs / 'sub').mkdir(parents=True)
            with text_pdf(['Aspirin lowers PTGS2.', 'See https://doi.org/10.1234/example.5']) as path:
                shutil.copy(path, pdfs / 'a.pdf')
                shutil.copy(path, pdfs / 'sub' / 'copy.PDF')          # duplicate content
            (pdfs / 'broken.pdf').write_bytes(b'%PDF-1.4 not really')
            stats = build_corpus(pdfs, Path(directory) / 'corpus', repo, Path(directory) / 'cache', resolve=False,
                                 log=lambda *a: None)
            corpus = Path(directory) / 'corpus'
            documents = json.loads((corpus / 'documents.json').read_text())
            policy = json.loads((corpus / 'policy.json').read_text())
            index = json.loads((corpus / 'pdf_index.json').read_text())
            id_map = (corpus / 'id_map.csv').read_text().splitlines()
        self.assertEqual((stats['documents'], stats['duplicates'], stats['unreadable']), (1, 1, 1))
        doc = documents[0]
        self.assertEqual((doc['id'], doc['source_sha256']), (doc['source_sha256'], next(iter(index))))
        self.assertEqual([b['text'] for b in doc['blocks']][0], 'Aspirin lowers PTGS2.')
        self.assertEqual([b['id'] for b in doc['blocks']], ['p1', 'p2'])
        self.assertIn('TREATS', policy['ontology']['predicates'])
        self.assertIsNone(policy['scope'])
        self.assertEqual(id_map[1].split(',')[3], '10.1234/example.5')

    def test_undecodable_page_is_skipped_not_the_whole_pdf(self):
        import importlib.util
        if importlib.util.find_spec('pypdf') is None:
            self.skipTest('pypdf not installed')
        import shutil
        import sys
        from unittest.mock import patch
        import pypdf
        from pypdf.errors import LimitReachedError
        repo = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo / 'tests'))
        from pdf_fixtures import text_pdf
        from kgb import corpus as corpus_mod
        original = pypdf.PageObject.extract_text

        def flaky(page, *args, **kwargs):
            text = original(page, *args, **kwargs)
            if 'second' in text:
                raise LimitReachedError('Limit reached while decompressing.')
            return text
        with tempfile.TemporaryDirectory() as directory:
            pdfs = Path(directory) / 'pdfs'
            pdfs.mkdir()
            with text_pdf(['first page text', 'second page text', 'third page text']) as path:
                shutil.copy(path, pdfs / 'a.pdf')
            with patch.object(pypdf.PageObject, 'extract_text', flaky):
                stats = corpus_mod.build(pdfs, Path(directory) / 'corpus', repo, Path(directory) / 'cache',
                                         resolve=False, log=lambda *a: None)
            documents = json.loads((Path(directory) / 'corpus' / 'documents.json').read_text())
        self.assertEqual((stats['documents'], stats['unreadable'], stats['pages_skipped']), (1, 0, 1))
        self.assertEqual([b['id'] for b in documents[0]['blocks']], ['p1', 'p3'])

    def test_pypdf_limits_are_relaxed(self):
        import importlib.util
        if importlib.util.find_spec('pypdf') is None:
            self.skipTest('pypdf not installed')
        import pypdf
        from kgb.corpus import PYPDF_LIMIT_FACTOR, configure_pypdf
        before = pypdf.get_configuration().zlib_maximum_output_length
        configure_pypdf()
        self.assertEqual(pypdf.get_configuration().zlib_maximum_output_length, before * PYPDF_LIMIT_FACTOR)
        pypdf.overwrite_configuration(zlib_maximum_output_length=before)


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
