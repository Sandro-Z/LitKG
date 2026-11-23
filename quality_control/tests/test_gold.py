"""Gold-standard evaluation: hand-computed fixtures (run with discover -s quality_control/tests)."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from quality_control.engine import evaluate as review
from quality_control.gold import (evaluate, gold_id, norm_text, predicted_id_keys, spearman, list_profiles,
                                  load_profile, resolve_profile, review_all, validate_gold)
from quality_control.models import normalize
from quality_control.scoring import apply_gold, combine_human
from quality_control.server import Service, handler
from quality_control.sources import attach_litkg_sources
from quality_control.store import Store

TEXT_A = 'Aspirin reduces colon cancer. PTGS2 binds aspirin.'
TEXT_B = 'TP53 is associated with glioma. Glioma patients.'
TEXT_C = 'BRCA1 causes breast cancer.'


def ent(doc, text, start, kind, ids):
    full = {'biored:1': TEXT_A, 'biored:2': TEXT_B, 'biored:3': TEXT_C}[doc]
    assert full[start:start + len(text)] == text
    return dict(doc_id=doc, start=start, end=start + len(text), text=text, type=kind, ids=ids)


def biored_gold():
    return {
        'schema': 'litkg-gold/1', 'kind': 'biored', 'split': 'fixture', 'name': 'biored-fixture',
        'documents': [dict(id='biored:1', external_id='biored:1', title='Aspirin study', text=TEXT_A),
                      dict(id='biored:2', external_id='biored:2', title='TP53 in glioma', text=TEXT_B),
                      dict(id='biored:3', external_id='biored:3', title='BRCA1 report', text=TEXT_C)],
        'entities': [ent('biored:1', 'Aspirin', 0, 'ChemicalEntity', ['D001241']),
                     ent('biored:1', 'colon cancer', 16, 'DiseaseOrPhenotypicFeature', ['D003110']),
                     ent('biored:1', 'PTGS2', 30, 'GeneOrGeneProduct', ['5743']),
                     ent('biored:1', 'aspirin', 42, 'ChemicalEntity', ['D001241']),
                     ent('biored:2', 'TP53', 0, 'GeneOrGeneProduct', ['7157']),
                     ent('biored:2', 'glioma', 24, 'DiseaseOrPhenotypicFeature', ['D005910']),
                     ent('biored:2', 'Glioma', 32, 'DiseaseOrPhenotypicFeature', ['D005910']),
                     ent('biored:3', 'BRCA1', 0, 'GeneOrGeneProduct', ['672']),
                     ent('biored:3', 'breast cancer', 13, 'DiseaseOrPhenotypicFeature', ['D001943'])],
        'relations': [dict(doc_id='biored:1', id1='D001241', id2='D003110', type='Negative_Correlation', novelty='Novel'),
                      dict(doc_id='biored:1', id1='5743', id2='D001241', type='Bind', novelty='No'),
                      dict(doc_id='biored:2', id1='7157', id2='D005910', type='Association', novelty='Novel'),
                      dict(doc_id='biored:3', id1='672', id2='D001943', type='Positive_Correlation', novelty='Novel')],
        'sentences': [], 'labels': {'entity_types': [], 'relation_types': []}, 'meta': {}}


NODES = [
    dict(id='n-asp', label='Aspirin', types=['Drug'], xrefs=['MESH:D001241']),
    dict(id='n-cc', label='colon cancer', types=['Disease'], xrefs=['D003110']),
    dict(id='n-ptgs2', label='PTGS2', types=['Gene'], xrefs=['NCBIGene:5743']),
    dict(id='n-tp53', label='TP53', types=['Gene'], xrefs=['7157']),
    dict(id='n-glioma', label='Glioma', aliases=['glioma'], types=['Disease'], xrefs=['mesh:d005910']),
    dict(id='n-pat', label='patients', types=['StudyGroup']),
]


def claim(cid, s, p, o, doc, quote, **extra):
    return dict(id=cid, subject={'id': s}, predicate=p, object={'id': o}, document_id=doc,
                source_refs=[dict(doc_id=doc, block_id='b', quote=quote)], **extra)


def system_graph():
    return normalize({
        'name': 'system-a', 'nodes': copy.deepcopy(NODES),
        'claims': [
            claim('c1', 'n-asp', 'CORRELATED_WITH', 'n-cc', 'A', 'Aspirin reduces colon cancer.',
                  qualifiers={'association_direction': 'negative'}),
            claim('c2', 'n-ptgs2', 'ASSOCIATED_WITH', 'n-asp', 'A', 'PTGS2 binds aspirin.'),
            claim('c3', 'n-tp53', 'ASSOCIATED_WITH', 'n-glioma', 'x2', 'TP53 is associated with glioma.'),
            claim('c4', 'n-cc', 'CORRELATED_WITH', 'n-asp', 'A', 'Aspirin reduces colon cancer.',
                  qualifiers={'association_direction': 'positive'}, negated=True),
            claim('c5', 'n-tp53', 'ASSOCIATED_WITH', 'n-glioma', 'D', 'Unrelated.'),
            claim('c6', 'n-pat', 'ASSOCIATED_WITH', 'n-tp53', 'x2', 'Glioma patients.'),
        ],
        'documents': [dict(id='A', external_id='biored:1', title='Aspirin study', blocks=[dict(id='b', text=TEXT_A)]),
                      dict(id='x2', title='  tp53 IN glioma ', blocks=[dict(id='b', text=TEXT_B)]),
                      dict(id='D', title='Unrelated', blocks=[dict(id='b', text='Unrelated.')])]})


def gold_derived_graph(gold):
    """Build a graph straight from gold annotations; every metric must be 1.0."""
    predicate = {'Negative_Correlation': ('CORRELATED_WITH', {'association_direction': 'negative'}),
                 'Positive_Correlation': ('CORRELATED_WITH', {'association_direction': 'positive'}),
                 'Association': ('ASSOCIATED_WITH', {}), 'Bind': ('BINDS_TO', {})}
    nodes, claims = {}, []
    types = {'ChemicalEntity': 'Drug', 'DiseaseOrPhenotypicFeature': 'Disease', 'GeneOrGeneProduct': 'Gene'}
    for e in gold['entities']:
        key = (e['doc_id'], e['ids'][0])
        nodes.setdefault(key, dict(id=f'{e["doc_id"]}|{e["ids"][0]}', label=e['text'], types=[types[e['type']]],
                                   xrefs=list(e['ids'])))
    for i, r in enumerate(gold['relations']):
        name, qualifiers = predicate[r['type']]
        claims.append(dict(id=f'g{i}', subject={'id': nodes[(r['doc_id'], r['id1'])]['id']}, predicate=name,
                           object={'id': nodes[(r['doc_id'], r['id2'])]['id']}, qualifiers=qualifiers,
                           document_id=r['doc_id']))
    documents = [dict(id=d['id'], external_id=d['external_id'], title=d['title'], blocks=[dict(id='ta', text=d['text'])])
                 for d in gold['documents']]
    return normalize(dict(name='gold-derived', nodes=list(nodes.values()), claims=claims, documents=documents))


class IdentifierTests(unittest.TestCase):
    def test_namespace_prefix_normalization(self):
        self.assertEqual(gold_id('D001234', 'ChemicalEntity'), 'MESH:D001234')
        self.assertIn('mesh:d001234', predicted_id_keys('MESH:D001234'))
        self.assertIn('mesh:d001234', predicted_id_keys('D001234'))
        self.assertIn('mesh:d001234', predicted_id_keys('mesh : d001234'))
        self.assertEqual(gold_id('6331', 'GeneOrGeneProduct'), 'NCBIGene:6331')
        self.assertIn('ncbigene:6331', predicted_id_keys('NCBIGene:6331'))
        self.assertIn('ncbigene:6331', predicted_id_keys('Gene:6331'))
        self.assertIn('ncbigene:6331', predicted_id_keys('6331'))
        self.assertEqual(gold_id('9606', 'OrganismTaxon'), 'NCBITaxon:9606')
        self.assertIn('ncbitaxon:9606', predicted_id_keys('NCBITaxon:9606'))
        self.assertEqual(gold_id('OMIM:192500', 'DiseaseOrPhenotypicFeature'), 'OMIM:192500')
        self.assertEqual(gold_id('rs1042522', 'SequenceVariant'), 'dbSNP:rs1042522')
        self.assertIn('dbsnp:rs1042522', predicted_id_keys('dbSNP:rs1042522'))
        self.assertEqual(gold_id('p|SUB|V|1763|M', 'SequenceVariant'), 'tmVar:p|SUB|V|1763|M')
        self.assertIn('tmvar:p|sub|v|1763|m', predicted_id_keys('p|SUB|V|1763|M'))
        self.assertIsNone(gold_id('-', 'ChemicalEntity'))
        self.assertEqual(norm_text('  “Colon  Cancer”. '), 'colon cancer')

    def test_spearman_matches_hand_computation(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40])['rho'], 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3], [3, 2, 1])['rho'], -1.0)
        # ranks x=[1,2.5,2.5,4], y=[1,2,3,4] -> rho = 4.5 / sqrt(4.5*5)
        self.assertAlmostEqual(spearman([1, 2, 2, 3], [1, 2, 3, 4])['rho'], 4.5 / (4.5 * 5) ** 0.5)
        self.assertEqual(spearman([1, 2], [1, 2]), {'rho': None, 'n': 2})


class BioredTests(unittest.TestCase):
    def run_eval(self, graph, gold=None, modes=('id', 'mention'), **kw):
        return evaluate(graph, gold or biored_gold(), 'biored', 'litkg', modes, bootstrap_rounds=kw.pop('rounds', 200), **kw)

    def test_hand_computed_micro_macro_and_alignment(self):
        report, errors = self.run_eval(system_graph())
        alignment = report['alignment']
        self.assertEqual((alignment['aligned'], alignment['gold_documents'], alignment['unaligned_graph_documents']), (2, 3, 1))
        self.assertEqual(alignment['by_method'], {'external_id': 1, 'title': 1})
        self.assertAlmostEqual(alignment['coverage'], 2 / 3)
        for mode in ('id', 'mention'):
            with self.subTest(mode=mode):
                result = report['results'][mode]
                micro = result['metrics']['micro']
                # Unaligned gold doc biored:3 is not in any denominator: gold pairs = 3, not 4.
                self.assertEqual((micro['pair']['tp'], micro['pair']['n_pred'], micro['pair']['n_gold']), (3, 3, 3))
                self.assertEqual(micro['pair']['f1'], 1.0)
                self.assertEqual((micro['pair_type']['tp'], micro['pair_type']['n_pred'], micro['pair_type']['n_gold']), (2, 3, 3))
                self.assertAlmostEqual(micro['pair_type']['precision'], 2 / 3)
                self.assertAlmostEqual(micro['pair_type']['f1'], 2 / 3)
                per_type = result['metrics']['per_type']
                self.assertEqual(per_type['Association']['precision'], 0.5)
                self.assertEqual(per_type['Association']['recall'], 1.0)
                self.assertEqual((per_type['Bind']['precision'], per_type['Bind']['recall']), (None, 0.0))
                macro = result['metrics']['macro']['pair_type']
                self.assertEqual((macro['precision']['value'], macro['recall']['value'], macro['f1']['value']), (0.75, 0.75, 0.75))
                self.assertEqual(macro['f1']['n'], 2)
                category = result['metrics']['per_entity_pair']['ChemicalEntity–GeneOrGeneProduct']['pair_type']
                self.assertEqual((category['tp'], category['n_pred'], category['n_gold']), (0, 1, 1))
                claims = result['claims']
                self.assertEqual((claims['total'], claims['outside_aligned_documents'], claims['negated_excluded'],
                                  claims['considered'], claims['endpoint_unaligned'], claims['endpoint_aligned']),
                                 (6, 1, 1, 4, 1, 3))
                self.assertEqual(result['metrics']['endpoint_alignment']['resolved'], 7)
                self.assertEqual(result['rule_hits']['corr-neg:mapped'], 1)
                fp = errors[mode]['fp']
                self.assertEqual([(e['label'], e['reason'], e['gold_types_for_pair']) for e in fp],
                                 [('Association', 'wrong_type', ['Bind'])])
                self.assertEqual(fp[0]['claims'][0]['quotes'], ['PTGS2 binds aspirin.'])
                self.assertEqual([e['label'] for e in errors[mode]['fn']], ['Bind'])
        self.assertEqual(report['results']['id']['metrics']['endpoint_alignment']['no_identifier'], 1)
        intervals = report['results']['mention']['metrics']['bootstrap']['intervals']['pair_type']['f1']
        self.assertLessEqual(intervals['low'], 2 / 3)
        self.assertGreaterEqual(intervals['high'], 2 / 3)
        again, _ = self.run_eval(system_graph())
        self.assertEqual(again['results'], report['results'])

    def test_negated_claims_are_excluded(self):
        report, _ = self.run_eval(system_graph(), modes=('mention',))
        result = report['results']['mention']
        self.assertEqual(result['claims']['negated_excluded'], 1)
        # c4 would have added (Aspirin, colon cancer, Positive_Correlation) as a false positive.
        self.assertEqual(result['metrics']['micro']['pair_type']['n_pred'], 3)
        self.assertNotIn('Positive_Correlation', result['metrics']['per_type'])

    def test_gold_derived_graph_scores_one(self):
        gold = biored_gold()
        report, errors = self.run_eval(gold_derived_graph(gold), gold)
        self.assertEqual(report['alignment']['coverage'], 1.0)
        for mode in ('id', 'mention'):
            micro = report['results'][mode]['metrics']['micro']
            for key in ('pair', 'pair_type'):
                self.assertEqual((micro[key]['precision'], micro[key]['recall'], micro[key]['f1']), (1.0, 1.0, 1.0))
            self.assertEqual(errors[mode]['fp'], [])
            self.assertEqual(errors[mode]['fn'], [])

    def test_empty_graph_has_zero_recall(self):
        gold = biored_gold()
        graph = normalize(dict(claims=[], documents=[dict(id=d['id'], external_id=d['id'], blocks=[dict(id='ta', text=d['text'])])
                                                     for d in gold['documents']]))
        report, _ = self.run_eval(graph, gold)
        micro = report['results']['mention']['metrics']['micro']['pair']
        self.assertEqual((micro['recall'], micro['precision'], micro['f1'], micro['n_gold']), (0.0, None, 0.0, 4))
        macro = report['results']['mention']['metrics']['macro']['pair']
        self.assertEqual((macro['recall'], macro['f1']), ({'value': 0.0, 'n': 3}, {'value': 0.0, 'n': 3}))

    def test_ambiguous_mentions_do_not_count(self):
        gold = biored_gold()
        gold['documents'] = [dict(id='biored:9', external_id='biored:9', title='P450', text='P450 causes hepatitis.')]
        gold['entities'] = [dict(doc_id='biored:9', start=0, end=4, text='P450', type='GeneOrGeneProduct', ids=['1543', '1548']),
                            dict(doc_id='biored:9', start=12, end=21, text='hepatitis', type='DiseaseOrPhenotypicFeature', ids=['D006505'])]
        gold['relations'] = [dict(doc_id='biored:9', id1='1543', id2='D006505', type='Association', novelty='No')]
        graph = normalize(dict(nodes=[dict(id='p', label='P450', types=['Gene']), dict(id='h', label='hepatitis', types=['Disease'])],
                               claims=[dict(id='c', subject={'id': 'p'}, predicate='ASSOCIATED_WITH', object={'id': 'h'},
                                            document_id='biored:9')],
                               documents=[dict(id='biored:9', blocks=[dict(id='ta', text='P450 causes hepatitis.')])]))
        report, _ = self.run_eval(graph, gold, modes=('mention',))
        result = report['results']['mention']
        self.assertEqual(result['metrics']['endpoint_alignment']['ambiguous'], 1)
        self.assertEqual(result['metrics']['micro']['pair']['tp'], 0)
        self.assertEqual(result['claims']['endpoint_unaligned'], 1)

    def test_review_levels_control_needs_review_rules(self):
        graph = normalize(dict(nodes=copy.deepcopy(NODES[:2]), claims=[
            dict(id='t', subject={'id': 'n-asp'}, predicate='TREATS', object={'id': 'n-cc'}, document_id='A')],
            documents=[dict(id='A', external_id='biored:1', blocks=[dict(id='b', text=TEXT_A)])]))
        loose, _ = self.run_eval(graph, modes=('mention',))
        strict, _ = self.run_eval(graph, modes=('mention',), review_levels=['confirmed'])
        self.assertEqual(loose['results']['mention']['metrics']['micro']['pair_type']['tp'], 1)
        self.assertEqual(strict['results']['mention']['claims']['excluded_review_predicate'], 1)
        self.assertEqual(strict['results']['mention']['metrics']['micro']['pair_type']['n_pred'], 0)

    def test_inputs_are_not_modified(self):
        graph, gold = system_graph(), biored_gold()
        before = (json.dumps(graph, sort_keys=True), json.dumps(gold, sort_keys=True))
        self.run_eval(graph, gold)
        self.assertEqual(before, (json.dumps(graph, sort_keys=True), json.dumps(gold, sort_keys=True)))


DIMBRE_TEXT = 'Inulin boosts Bifidobacterium . It may lower plasma glucose .'


def dimbre_gold():
    return {
        'schema': 'litkg-gold/1', 'kind': 'dimbre', 'split': 'fixture', 'name': 'dimbre-fixture',
        'documents': [dict(id='dimbre:1', external_id='dimbre:1', title='Inulin boosts Bifidobacterium .', text=DIMBRE_TEXT)],
        'entities': [dict(doc_id='dimbre:1', start=0, end=6, text='Inulin', type='Nutrient', ids=[]),
                     dict(doc_id='dimbre:1', start=14, end=29, text='Bifidobacterium', type='Microorganism', ids=[]),
                     dict(doc_id='dimbre:1', start=45, end=59, text='plasma glucose', type='Chemical', ids=[])],
        'relations': [dict(doc_id='dimbre:1', sentence_index=0, sentence_indices=[0], head_span=[0, 6], tail_span=[14, 29],
                           head_text='Inulin', tail_text='Bifidobacterium', type='INCREASES', factuality='Factual'),
                      dict(doc_id='dimbre:1', sentence_index=1, sentence_indices=[0, 1], head_span=[0, 6], tail_span=[45, 59],
                           head_text='Inulin', tail_text='plasma glucose', type='DECREASES', factuality='Unknown')],
        'sentences': [dict(doc_id='dimbre:1', index=0, start=0, end=31), dict(doc_id='dimbre:1', index=1, start=32, end=61)],
        'labels': {}, 'meta': {}}


def dimbre_graph():
    def c(cid, s, p, o, quote, direction=None, **extra):
        obj = {'id': o}
        if direction:
            obj['process'] = {'kind': 'abundance', 'direction': direction}
        return dict(id=cid, subject={'id': s}, predicate=p, object=obj, document_id='d',
                    source_refs=[dict(doc_id='d', block_id='text', quote=quote)], **extra)
    return normalize(dict(
        nodes=[dict(id='inulin', label='Inulin', types=['DietaryComponent']),
               dict(id='bifido', label='Bifidobacterium', types=['MicrobialTaxon']),
               dict(id='glucose', label='glucose', types=['Metabolite'])],
        claims=[c('d1', 'inulin', 'AFFECTS', 'bifido', 'Inulin boosts Bifidobacterium .', 'increased'),
                c('d2', 'inulin', 'AFFECTS', 'glucose', 'It may lower plasma glucose .', 'decreased',
                  qualifiers={'speculative': True}),
                c('d3', 'bifido', 'ASSOCIATED_WITH', 'inulin', 'Inulin boosts Bifidobacterium .'),
                c('d4', 'inulin', 'AFFECTS', 'glucose', 'Text that is not in the abstract.', 'decreased')],
        documents=[dict(id='d', external_id='dimbre:1', blocks=[dict(id='text', text=DIMBRE_TEXT)])]))


class DimbreTests(unittest.TestCase):
    def test_strict_and_relaxed_hand_computed(self):
        report, errors = evaluate(dimbre_graph(), dimbre_gold(), 'dimbre', 'litkg')
        result = report['results']['span']
        strict, relaxed = result['metrics']['strict'], result['metrics']['relaxed']
        self.assertEqual(result['metrics']['location_coverage']['located'], 3)
        self.assertEqual(result['metrics']['location_coverage']['considered'], 4)
        # strict: d1 TP; d2 "glucose" != "plasma glucose"; d3 reversed non-symmetric -> FP.
        self.assertEqual((strict['relation_type']['tp'], strict['relation_type']['n_pred'], strict['relation_type']['n_gold']), (1, 3, 2))
        self.assertAlmostEqual(strict['relation_type']['f1'], 0.4)
        self.assertEqual((relaxed['relation_type']['tp'], relaxed['relation_type']['n_pred']), (2, 3))
        self.assertAlmostEqual(relaxed['relation_type']['f1'], 0.8)
        self.assertEqual(strict['factuality'], {'accuracy': 1.0, 'n': 1, 'confusion': {'Factual': {'Factual': 1}}})
        self.assertEqual((relaxed['factuality']['accuracy'], relaxed['factuality']['n']), (1.0, 2))
        self.assertEqual(relaxed['per_type']['DECREASES']['recall'], 1.0)
        self.assertEqual([e['reason'] for e in errors['relaxed']['fp']], ['no_gold_relation'])
        self.assertEqual([e['label'] for e in errors['strict']['fn']], ['DECREASES'])
        self.assertEqual(errors['unlocated'][0]['claim_id'], 'd4')

    def test_gold_offsets_from_text_chunks_are_used(self):
        graph = dimbre_graph()
        graph['documents'][0]['blocks'] = [dict(id='chunk2', text=DIMBRE_TEXT[32:], source_offset=32)]
        for c in graph['claims']:
            c['source_refs'] = [dict(doc_id='d', block_id='chunk2', start=0, end=29, quote=DIMBRE_TEXT[32:61])]
        report, _ = evaluate(graph, dimbre_gold(), 'dimbre', 'litkg')
        self.assertEqual(report['results']['span']['claims']['located'], 4)


class ProfileTests(unittest.TestCase):
    def test_profiles_load_and_every_rule_has_a_review_state(self):
        names = {p['name'] for p in list_profiles()}
        self.assertEqual(names, {'litkg', 'pubtator3', 'neo4j-builder'})
        profile, profile_hash = load_profile('litkg')
        self.assertEqual(len(profile_hash), 64)
        for spec in profile['benchmarks'].values():
            for rule in spec['relation_rules'] + spec['entity_types']:
                self.assertIn(rule['review'], {'confirmed', 'needs_review', 'TODO'})
        with self.assertRaises(ValueError):
            load_profile('../litkg')

    def test_gold_validation_rejects_bad_offsets(self):
        gold = biored_gold()
        gold['entities'][0]['start'] = 1
        with self.assertRaisesRegex(ValueError, 'GOLD_ENTITY_OFFSET_INVALID'):
            validate_gold('biored', gold)
        with self.assertRaisesRegex(ValueError, 'GOLD_PAYLOAD_INVALID'):
            validate_gold('dimbre', biored_gold())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'qc.sqlite3')
        self.service = Service(self.store)
        self.service.gold_dir = Path(self.tmp.name) / 'benchmarks'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.service))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, data=None):
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait(self, run_id):
        for _ in range(500):
            status, run = self.call(f'/api/runs/{run_id}')
            if run['status'] not in {'queued', 'running'}:
                return run
            time.sleep(.02)
        self.fail('review did not finish')

    def test_every_review_runs_the_gold_standard_and_scores_include_it(self):
        self.service.gold_dir.mkdir()
        (self.service.gold_dir / 'gold.biored-fixture.json').write_text(json.dumps(biored_gold()))
        graph_a = self.store.add_graph(system_graph(), {})
        graph_b = self.store.add_graph(gold_derived_graph(biored_gold()), {})
        llm_run = self.store.start_run(graph_a, 'llm')
        self.store.update_run(llm_run, 'completed', {'assessments': [
            {'claim_id': 'c1', 'verdict': 'SUPPORTED'}, {'claim_id': 'c2', 'verdict': 'SUPPORTED'},
            {'claim_id': 'c3', 'verdict': 'UNSUPPORTED'}], 'candidates': [], 'matches': []})
        snapshot_before = self.store.graph(graph_a)
        status, run = self.call(f'/api/graphs/{graph_a}/runs', {'mode': 'automatic'})
        self.assertEqual(status, 202, run)
        run = self.wait(run['id'])
        self.assertEqual(self.store.graph(graph_a), snapshot_before)
        report = run['report']
        gold = report['gold']
        self.assertEqual((gold['status'], gold['profile']['name'], gold['profile']['source']), ('applied', 'litkg', 'default'))
        self.assertEqual(gold['calibration_run_id'], llm_run)   # a local rerun calibrates on the last LLM review
        pooled = gold['pooled']
        self.assertEqual((pooled['tp'], pooled['n_pred'], pooled['n_gold']), (2, 3, 3))
        self.assertEqual((pooled['judged_claims'], pooled['total_claims'], pooled['aligned_graph_documents']), (3, 6, 2))
        # No LLM evidence in a local run: both dimensions come from the gold standard alone.
        self.assertIsNone(report['machine_dimensions']['correctness'])
        self.assertEqual(report['dimensions']['correctness'], 66.67)
        self.assertEqual(report['dimensions']['completeness'], 66.67)
        self.assertEqual(report['dimension_details']['correctness']['gold_method'], 'gold_only')
        self.assertIsNotNone(report['scenarios']['field_review']['score'])
        entry = gold['sets'][0]
        calibration = entry['calibration']['mention']
        self.assertEqual(calibration['confusion']['TP']['SUPPORTED'], 1)
        self.assertEqual(calibration['confusion']['TP']['UNSUPPORTED'], 1)
        self.assertEqual(calibration['confusion']['FP']['SUPPORTED'], 1)
        self.assertEqual(calibration['p_supported_given_tp'], {'value': 0.5, 'n': 2})
        self.assertEqual(calibration['p_supported_given_fp'], {'value': 1.0, 'n': 1})
        self.assertEqual(set(entry['modes']), {'id', 'mention'})
        status, stored = self.call(f'/api/gold-evals/{entry["gold_eval_id"]}')
        self.assertEqual((stored['report']['trigger'], stored['report']['review_run_id'], stored['report']['qc_run_id']),
                         ('local_review', run['id'], llm_run))
        self.assertEqual(stored['report']['results']['mention']['calibration']['spearman_correctness_vs_gold_precision']['n'], 2)
        status, errors = self.call(f'/api/gold-evals/{entry["gold_eval_id"]}/errors')
        self.assertEqual(errors['errors']['mention']['fp'][0]['claims'][0]['claim_id'], 'c2')
        status, run_b = self.call(f'/api/graphs/{graph_b}/runs', {'mode': 'automatic'})
        run_b = self.wait(run_b['id'])
        self.assertEqual((run_b['report']['gold']['pooled']['precision'], run_b['report']['gold']['pooled']['recall']), (1.0, 1.0))
        self.assertIsNone(run_b['report']['gold']['sets'][0]['calibration']['mention'])
        status, sets = self.call('/api/gold-sets')
        self.assertEqual([g['name'] for g in sets['gold_sets']], ['biored-fixture'])   # imported once, by hash
        status, listing = self.call(f'/api/gold-evals?gold_set_id={sets["gold_sets"][0]["id"]}')
        self.assertEqual(len(listing['evals']), 2)
        status, overview = self.call('/api/graphs?view=overview')
        compact = {g['id']: g['latest_run']['report']['gold'] for g in overview['graphs']}
        self.assertEqual(compact[graph_a]['pooled']['f1'], pooled['f1'])
        self.assertNotIn('modes', compact[graph_a]['sets'][0])
        status, config = self.call('/api/config')
        self.assertEqual((config['gold_files'], config['gold_sets']), (1, 1))
        status, body = self.call(f'/api/graphs/{graph_a}/gold-evals', {'gold_set_id': sets['gold_sets'][0]['id']})
        self.assertEqual(status, 404)   # no separate gold run any more
        with self.store.connect() as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE gold_sets SET name='changed'")
        status, _ = self.call(f'/api/graphs/{graph_a}/delete', {'confirm_graph_id': graph_a})
        self.assertEqual(status, 200)

    def test_review_without_gold_or_with_broken_gold_files_keeps_working(self):
        graph = self.store.add_graph(system_graph(), {})
        status, run = self.call(f'/api/graphs/{graph}/runs', {'mode': 'automatic'})
        run = self.wait(run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['report']['gold']['status'], 'no_gold_sets')
        self.assertIsNone(run['report']['gold_fusion'])
        self.service.gold_dir.mkdir()
        (self.service.gold_dir / 'gold.biored-broken.json').write_text('{not json')
        status, run = self.call(f'/api/graphs/{graph}/runs', {'mode': 'automatic'})
        run = self.wait(run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['report']['gold']['file_problems'], [{'file': 'gold.biored-broken.json', 'code': 'GOLD_PAYLOAD_INVALID'}])

    def test_server_side_gold_files_are_listed_and_imported(self):
        status, body = self.call('/api/gold-files')
        self.assertEqual((status, body), (200, {'files': [], 'directory_configured': False}))
        self.service.gold_dir.mkdir()
        (self.service.gold_dir / 'gold.biored-fixture.json').write_text(json.dumps(biored_gold()))
        (self.service.gold_dir / 'documents.biored-fixture.json').write_text('[]')
        (self.service.gold_dir / 'notes.txt').write_text('ignored')
        status, body = self.call('/api/gold-files')
        self.assertEqual([(f['file'], f['kind'], f['split']) for f in body['files']],
                         [('gold.biored-fixture.json', 'biored', 'fixture')])
        status, created = self.call('/api/gold-sets', {'server_file': 'gold.biored-fixture.json'})
        self.assertEqual((status, created['created'], created['summary']['documents']), (201, True, 3))
        status, again = self.call('/api/gold-sets', {'kind': 'biored', 'payload': biored_gold()})
        self.assertEqual((status, again['id']), (200, created['id']))   # same content as the upload path
        self.assertEqual(self.store.list_gold_sets()[0]['name'], 'biored-fixture')
        for name, code in (('../gold.biored-fixture.json', 'GOLD_FILE_INVALID'), ('gold.biored-missing.json', 'GOLD_FILE_NOT_FOUND'),
                           ('documents.biored-fixture.json', 'GOLD_FILE_INVALID')):
            status, body = self.call('/api/gold-sets', {'server_file': name})
            self.assertEqual((status, body['error']), (400, code))
        status, body = self.call('/api/gold-sets', {'server_file': 'gold.biored-fixture.json', 'kind': 'dimbre'})
        self.assertEqual((status, body['error']), (400, 'GOLD_PAYLOAD_INVALID'))

    def test_invalid_requests(self):
        status, body = self.call('/api/gold-evals/' + '0' * 32)
        self.assertEqual(status, 404)
        status, body = self.call('/api/gold-sets', {'kind': 'biored', 'payload': {'schema': 'x'}})
        self.assertEqual((status, body['error']), (400, 'GOLD_PAYLOAD_INVALID'))


def merged_graph():
    """BioRED fixture documents plus the DiMB-RE fixture document in one graph."""
    graph, other = system_graph(), dimbre_graph()
    for key in ('nodes', 'claims', 'documents'):
        graph[key] = graph[key] + other[key]
    return graph


def gold_sets(*items):
    return [(f'id-{i}', gold['kind'], gold['name'], gold) for i, gold in enumerate(items)]


class ReviewAllTests(unittest.TestCase):
    def test_every_aligned_gold_set_is_evaluated_and_primary_metrics_are_pooled(self):
        summary, evaluations = review_all(merged_graph(), gold_sets(biored_gold(), dimbre_gold()), bootstrap_rounds=50)
        self.assertEqual(summary['status'], 'applied')
        self.assertEqual([(s['name'], s['primary_mode'], s['primary_metric']) for s in summary['sets']],
                         [('biored-fixture', 'mention', 'pair_type'), ('dimbre-fixture', 'relaxed', 'relation_type')])
        self.assertEqual(set(summary['sets'][0]['modes']), {'id', 'mention'})
        self.assertEqual(set(summary['sets'][1]['modes']), {'strict', 'relaxed'})
        self.assertEqual([e[0] for e in evaluations], ['id-0', 'id-1'])
        pooled = summary['pooled']
        # BioRED mention pair+type 2/3/3 plus DiMB-RE relaxed relation type 2/3/2.
        self.assertEqual((pooled['tp'], pooled['n_pred'], pooled['n_gold']), (4, 6, 5))
        self.assertEqual((pooled['precision'], pooled['recall']), (4 / 6, 4 / 5))
        self.assertEqual((pooled['judged_claims'], pooled['total_claims']), (6, 10))
        self.assertEqual((pooled['aligned_graph_documents'], pooled['graph_documents']), (3, 4))
        self.assertEqual(pooled['sets'], ['biored-fixture', 'dimbre-fixture'])

    def test_unaligned_and_overlapping_gold_sets(self):
        copy_ = biored_gold()
        copy_['name'] = 'bc8-train'
        copy_['meta'] = {'note': 'same documents, different file'}
        summary, evaluations = review_all(system_graph(), gold_sets(biored_gold(), copy_, dimbre_gold()), bootstrap_rounds=0)
        self.assertEqual([s['reason'] for s in summary['skipped']], ['no_aligned_documents'])
        self.assertEqual(len(evaluations), 2)
        # Equal coverage: name order decides, so bc8-train counts and biored-fixture is reported but not pooled.
        self.assertEqual([(s['name'], s['pooled']) for s in summary['sets']], [('bc8-train', True), ('biored-fixture', False)])
        self.assertIn('不重复计入', summary['sets'][1]['pool_note'])
        self.assertEqual((summary['pooled']['tp'], summary['pooled']['n_pred'], summary['pooled']['n_gold']), (2, 3, 3))
        none, _ = review_all(system_graph(), gold_sets(dimbre_gold()))
        self.assertEqual((none['status'], none['pooled']), ('not_applicable', None))
        empty, _ = review_all(system_graph(), [])
        self.assertEqual(empty['status'], 'no_gold_sets')

    def test_profile_follows_policy_then_producing_system(self):
        graph = system_graph()
        self.assertEqual(resolve_profile(graph), ('litkg', 'default'))
        graph['metadata'] = {'source': 'litkg', 'knowledge_base_id': 3}
        self.assertEqual(resolve_profile(graph), ('litkg', 'metadata.source'))
        graph['metadata'] = {'system': 'PubTator3+LLM'}
        self.assertEqual(resolve_profile(graph), ('pubtator3', 'metadata.system'))
        graph['metadata'] = {'system': 'Neo4j LLM Graph Builder'}
        self.assertEqual(resolve_profile(graph), ('neo4j-builder', 'metadata.system'))
        graph['policy']['gold_profile'] = 'pubtator3'
        self.assertEqual(resolve_profile(graph), ('pubtator3', 'policy.gold_profile'))
        graph['policy']['gold_profile'] = 'missing-profile'
        summary, evaluations = review_all(graph, gold_sets(biored_gold()))
        self.assertEqual((summary['status'], summary['code'], evaluations), ('failed', 'GOLD_PROFILE_NOT_FOUND', []))


class GoldScoringTests(unittest.TestCase):
    def report(self, gold):
        return dict(dimensions=dict(specialization=80, granularity=80, correctness=90, completeness=40, ontology=None,
                                    coverage=None), gold=gold, complete=False, gate='INCOMPLETE')

    def applied(self, precision=.5, recall=.8, claim_coverage=.5, document_coverage=1.0):
        return dict(status='applied', pooled=dict(precision=precision, recall=recall, claim_coverage=claim_coverage,
                                                  document_coverage=document_coverage))

    def test_weights_scale_with_coverage_and_human_fusion_comes_after(self):
        graph = system_graph()
        report = apply_gold(self.report(self.applied()), graph)
        self.assertEqual(report['machine_dimensions']['correctness'], 90)
        self.assertEqual(report['dimensions']['correctness'], 80.0)    # 0.75 x 90 + 0.25 x 50
        self.assertEqual(report['dimensions']['completeness'], 60.0)   # 0.5 x 40 + 0.5 x 80
        self.assertEqual(report['gold_fusion']['correctness']['effective_weight'], .25)
        again = apply_gold(copy.deepcopy(report), graph)
        self.assertEqual(again['dimensions'], report['dimensions'])    # idempotent
        state = dict(metrics=dict(score=0, decided=1, uncertain_votes=0, disagreements=0), rounds=[{'claim_ids': ['c1']}],
                     consensus=[], reviews=[])
        human = combine_human(report, state, graph)
        self.assertEqual(human['dimensions']['correctness'], 56.0)     # 0.7 x 80 + 0.3 x 0
        self.assertEqual(human['dimension_details']['correctness']['machine_score'], 90)
        self.assertEqual(human['dimension_details']['correctness']['pre_human_score'], 80.0)
        graph['policy']['gold_weight'] = 0
        self.assertEqual(apply_gold(self.report(self.applied()), graph)['dimensions']['correctness'], 90)

    def test_without_an_applicable_gold_set_scores_are_unchanged(self):
        graph = system_graph()
        for gold in (None, dict(status='not_applicable', pooled=None), dict(status='failed', code='GOLD_REVIEW_FAILED')):
            report = apply_gold(self.report(gold), graph)
            self.assertEqual(report['dimensions'], report['machine_dimensions'])
            self.assertIsNone(report['gold_fusion'])
        report = apply_gold(self.report(self.applied(precision=None)), graph)
        self.assertEqual(report['dimensions']['correctness'], 90)
        self.assertEqual(report['gold_fusion']['correctness']['method'], 'machine_only')

    def test_engine_runs_the_gold_review_and_explains_the_fused_score(self):
        graph = system_graph()
        seen = []

        def gold_review(report):
            seen.append(report['stages'])
            return review_all(graph, gold_sets(biored_gold()), qc_report=report, bootstrap_rounds=0)[0]
        report = review(graph, None, gold_review=gold_review)
        self.assertEqual(len(seen), 1)
        self.assertEqual(report['gold']['status'], 'applied')
        detail = report['dimension_details']['correctness']
        self.assertEqual((detail['score'], detail['gold_method'], detail['machine_score']), (66.67, 'gold_only', None))
        self.assertIn('金标准 precision', detail['reason'])
        self.assertNotIn('无人工金标准；候选命中率不是全图召回率或 F1', report['limitations'])
        self.assertEqual(report['machine_scenarios']['field_review']['score'],
                         review(graph)['scenarios']['field_review']['score'])


class LLMReviewGoldTests(unittest.TestCase):
    @staticmethod
    def wait(store, run_id):
        for _ in range(500):
            run = store.run(run_id)
            if run['status'] not in {'queued', 'running'}:
                return run
            time.sleep(.02)
        raise AssertionError('review did not finish')

    def test_llm_review_calibrates_on_its_own_verdicts(self):
        from quality_control.tests.test_quality import FakeLLM, fixture
        text = 'Alpha treats Beta. Gamma binds Beta.'
        gold = {'schema': 'litkg-gold/1', 'kind': 'biored', 'split': 'mini', 'name': 'biored-mini',
                'documents': [dict(id='d1', external_id='d1', title='Alpha', text=text)],
                'entities': [dict(doc_id='d1', start=0, end=5, text='Alpha', type='ChemicalEntity', ids=['D1']),
                             dict(doc_id='d1', start=13, end=17, text='Beta', type='DiseaseOrPhenotypicFeature', ids=['D2']),
                             dict(doc_id='d1', start=19, end=24, text='Gamma', type='GeneOrGeneProduct', ids=['7']),
                             dict(doc_id='d1', start=31, end=35, text='Beta', type='DiseaseOrPhenotypicFeature', ids=['D2'])],
                'relations': [dict(doc_id='d1', id1='D1', id2='D2', type='Negative_Correlation', novelty='No'),
                              dict(doc_id='d1', id1='7', id2='D2', type='Bind', novelty='No')],
                'sentences': [], 'labels': {}, 'meta': {}}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'qc.sqlite3')
            service = Service(store, FakeLLM)
            service.gold_dir = Path(directory) / 'gold'
            service.gold_dir.mkdir()
            (service.gold_dir / 'gold.biored-mini.json').write_text(json.dumps(gold))
            gid = store.add_graph(normalize(fixture()), {})
            rid = service.submit(gid, 'llm')['id']
            run = self.wait(store, rid)
            # A local rerun afterwards keeps the LLM evidence scores in every view and re-evaluates the gold standard.
            local = self.wait(store, service.submit(gid, 'automatic')['id'])
            page = store.latest_run_page(gid, 0)['report']
            overview = store.list_graph_overviews()[0]['latest_run']['report']
            service.pool.shutdown(wait=True)
        self.assertEqual(local['report']['gold']['sets'][0]['gold_eval_id'] != run['report']['gold']['sets'][0]['gold_eval_id'], True)
        for view in (local['report'], page, overview):
            self.assertEqual(view['machine_dimensions']['correctness'], 100)
            self.assertEqual(view['dimensions']['completeness'], 75.0)
            self.assertEqual(view['gold']['status'], 'applied')
        self.assertEqual(local['report']['gold']['calibration_run_id'], rid)
        self.assertEqual(run['status'], 'completed')
        report = run['report']
        self.assertEqual(report['gate'], 'PASS')   # the gate stays evidence-based
        self.assertEqual(report['gold']['calibration_run_id'], rid)
        calibration = report['gold']['sets'][0]['calibration']['mention']
        self.assertEqual(calibration['confusion']['TP']['SUPPORTED'], 1)
        self.assertEqual((report['gold']['pooled']['precision'], report['gold']['pooled']['recall']), (1.0, 0.5))
        self.assertEqual(report['machine_dimensions']['completeness'], 100)
        self.assertEqual(report['dimensions']['completeness'], 75.0)   # 0.5 x 100 + 0.5 x 50
        self.assertEqual(report['dimensions']['correctness'], 100.0)


class TextSourceAttachTests(unittest.TestCase):
    def test_litkg_text_original_is_attached_as_complete(self):
        stored = json.dumps({'schema': 'litkg-text-document/1', 'id': 'biored:1', 'external_id': 'biored:1',
                             'title': 'T', 'blocks': [{'id': 'ta', 'section': 'title+abstract', 'text': TEXT_A}]},
                            sort_keys=True, separators=(',', ':')).encode()
        sha = hashlib.sha256(stored).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / f'{sha}.litkg-text.json').write_bytes(stored)
            graph = normalize(dict(claims=[], documents=[dict(id='1', source_sha256=sha, source_kind='text',
                                                              external_id='biored:1', blocks=[dict(id='10', text=TEXT_A)])]))
            counts = attach_litkg_sources(graph, directory)
        self.assertEqual(counts, {'attached': 1})
        doc = graph['documents'][0]
        self.assertEqual(doc['parsing_status'], 'complete')
        self.assertEqual(doc['external_id'], 'biored:1')
        original = [b for b in doc['blocks'] if b.get('source_origin') == 'litkg_original']
        self.assertEqual((original[0]['text'], original[0]['source_block_id']), (TEXT_A, 'ta'))


if __name__ == '__main__':
    unittest.main()
