#!/usr/bin/env python3
"""Upper bound of BioRED metrics for a *perfect* extractor under each alignment mode.

Builds a graph directly from the gold annotations (one node per gold ID per
document, label = first mention, aliases = every mention text, xrefs = the
gold ID; one claim per gold relation with a predicate that the ``litkg``
profile maps back to the gold type) and evaluates it with
``quality_control.gold``.  Any shortfall from 1.0 is caused by the alignment
procedure or the profile, not by extraction.

    python3 benchmarks/mention_upper_bound.py data/benchmarks/gold.biored-test.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quality_control.gold import evaluate  # noqa: E402
from quality_control.models import normalize  # noqa: E402

PREDICATES = {
    'Negative_Correlation': ('CORRELATED_WITH', {'association_direction': 'negative'}),
    'Positive_Correlation': ('CORRELATED_WITH', {'association_direction': 'positive'}),
    'Association': ('ASSOCIATED_WITH', {}), 'Bind': ('BINDS_TO', {}),
}
TYPES = {'ChemicalEntity': 'Drug', 'DiseaseOrPhenotypicFeature': 'Disease', 'GeneOrGeneProduct': 'Gene',
         'OrganismTaxon': 'Organism', 'SequenceVariant': 'Mutation', 'CellLine': 'CellLine'}


def gold_graph(gold):
    nodes, claims, skipped = {}, [], {}
    for e in gold['entities']:
        for raw in e['ids']:
            if raw == '-':
                continue
            node = nodes.setdefault((e['doc_id'], raw), dict(
                id=f'{e["doc_id"]}|{raw}', label=e['text'], aliases=[], types=[TYPES[e['type']]], xrefs=[raw]))
            if e['text'] not in node['aliases'] and e['text'] != node['label']:
                node['aliases'].append(e['text'])
    for i, r in enumerate(gold['relations']):
        if r['type'] not in PREDICATES:
            skipped[r['type']] = skipped.get(r['type'], 0) + 1
            continue
        predicate, qualifiers = PREDICATES[r['type']]
        claims.append(dict(id=f'g{i}', subject={'id': nodes[(r['doc_id'], r['id1'])]['id']}, predicate=predicate,
                           object={'id': nodes[(r['doc_id'], r['id2'])]['id']}, qualifiers=qualifiers,
                           document_id=r['doc_id']))
    documents = [dict(id=d['id'], external_id=d['external_id'], title=d['title'], blocks=[dict(id='ta', text=d['text'])])
                 for d in gold['documents']]
    return normalize(dict(name='gold-derived upper bound', nodes=list(nodes.values()), claims=claims,
                          documents=documents)), skipped


def main(path):
    gold = json.loads(Path(path).read_text(encoding='utf-8'))
    if gold.get('kind') != 'biored':
        raise SystemExit('only BioRED gold files are supported')
    graph, skipped = gold_graph(gold)
    report, _ = evaluate(graph, gold, 'biored', 'litkg', ('id', 'mention'), bootstrap_rounds=0)
    print(f'{gold["name"]}: {len(gold["documents"])} documents, {len(gold["relations"])} gold relations; '
          f'relations without a LitKG predicate (not emitted): {skipped or 0}')
    for mode, result in report['results'].items():
        micro, ep = result['metrics']['micro'], result['metrics']['endpoint_alignment']
        print(f'  {mode:8s} endpoint coverage {ep["resolved"]}/{ep["total"]} '
              f'(ambiguous {ep["ambiguous"]}, unresolved {ep["unresolved"]}); '
              f'pair R {micro["pair"]["tp"]}/{micro["pair"]["n_gold"]} = {micro["pair"]["recall"]:.3f}; '
              f'pair+type R {micro["pair_type"]["tp"]}/{micro["pair_type"]["n_gold"]} = {micro["pair_type"]["recall"]:.3f}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
