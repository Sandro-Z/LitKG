"""Gold-standard evaluation: hand-computed fixtures (run with discover -s quality_control/tests)."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from quality_control.gold import (evaluate, gold_id, norm_text, predicted_id_keys, spearman, list_profiles,
                                  load_profile, validate_gold)
from quality_control.models import normalize
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
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(Service(self.store)))
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

    def test_two_systems_on_one_gold_set_with_calibration(self):
        status, gold_set = self.call('/api/gold-sets', {'kind': 'biored', 'name': 'fixture', 'payload': biored_gold()})
        self.assertEqual(status, 201)
        status, again = self.call('/api/gold-sets', {'kind': 'biored', 'name': 'copy', 'payload': biored_gold()})
        self.assertEqual((status, again['id'], again['created']), (200, gold_set['id'], False))
        graph_a = self.store.add_graph(system_graph(), {})
        graph_b = self.store.add_graph(gold_derived_graph(biored_gold()), {})
        run = self.store.start_run(graph_a, 'llm')
        self.store.update_run(run, 'completed', {'assessments': [
            {'claim_id': 'c1', 'verdict': 'SUPPORTED'}, {'claim_id': 'c2', 'verdict': 'SUPPORTED'},
            {'claim_id': 'c3', 'verdict': 'UNSUPPORTED'}], 'candidates': [], 'matches': []})
        snapshot_before = self.store.graph(graph_a)
        status, first = self.call(f'/api/graphs/{graph_a}/gold-evals',
                                  {'gold_set_id': gold_set['id'], 'profile': 'litkg', 'modes': 'both', 'bootstrap': 100})
        self.assertEqual(status, 201, first)
        self.assertEqual(self.store.graph(graph_a), snapshot_before)
        calibration = first['report']['results']['mention']['calibration']
        self.assertEqual(calibration['confusion']['TP']['SUPPORTED'], 1)
        self.assertEqual(calibration['confusion']['TP']['UNSUPPORTED'], 1)
        self.assertEqual(calibration['confusion']['FP']['SUPPORTED'], 1)
        self.assertEqual(calibration['p_supported_given_tp'], {'value': 0.5, 'n': 2})
        self.assertEqual(calibration['p_supported_given_fp'], {'value': 1.0, 'n': 1})
        self.assertEqual(calibration['spearman_correctness_vs_gold_precision']['n'], 2)
        self.assertEqual(first['report']['qc_run_id'], run)
        status, second = self.call(f'/api/graphs/{graph_b}/gold-evals',
                                   {'gold_set_id': gold_set['id'], 'profile': 'pubtator3', 'modes': ['mention'], 'bootstrap': 0})
        self.assertEqual(status, 201, second)
        self.assertIsNone(second['report']['results']['mention']['calibration'])
        status, listing = self.call(f'/api/gold-evals?gold_set_id={gold_set["id"]}')
        self.assertEqual(len(listing['evals']), 2)
        self.assertEqual(len({e['report']['gold_hash'] for e in listing['evals']}), 1)
        status, errors = self.call(f'/api/gold-evals/{first["id"]}/errors')
        self.assertEqual(errors['errors']['mention']['fp'][0]['claims'][0]['claim_id'], 'c2')
        status, graph_list = self.call(f'/api/graphs/{graph_a}/gold-evals')
        self.assertEqual([e['id'] for e in graph_list['evals']], [first['id']])
        status, profiles = self.call('/api/gold-profiles')
        self.assertIn('litkg', {p['name'] for p in profiles['profiles']})
        with self.store.connect() as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE gold_sets SET name='changed'")
        status, _ = self.call(f'/api/graphs/{graph_a}/delete', {'confirm_graph_id': graph_a})
        self.assertEqual(status, 200)

    def test_invalid_requests(self):
        graph = self.store.add_graph(system_graph(), {})
        status, body = self.call(f'/api/graphs/{graph}/gold-evals', {'gold_set_id': 'missing'})
        self.assertEqual(status, 404)
        status, body = self.call('/api/gold-sets', {'kind': 'biored', 'payload': {'schema': 'x'}})
        self.assertEqual((status, body['error']), (400, 'GOLD_PAYLOAD_INVALID'))


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
