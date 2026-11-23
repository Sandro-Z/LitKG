"""Gold-standard (BioRED / DiMB-RE) evaluation of any imported graph snapshot.

System-neutral and read-only: it reads a normalized graph snapshot, an
immutable gold set (from ``benchmarks/prepare.py``) and a JSON mapping
profile, and returns a report.  It never writes to the graph.

Every review run calls ``review_all`` with all stored gold sets; the pooled
precision / recall it returns is folded into correctness / completeness by
``scoring.apply_gold``.  Graphs whose documents align with no gold set are
scored exactly as before.

Standard library only.
"""
import json
import math
import random
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from .models import at, digest

GOLD_EVAL_VERSION = 'qc-gold/1.1.0'
GOLD_SCHEMA = 'litkg-gold/1'
KINDS = {'biored', 'dimbre'}
MODES = {'id', 'mention'}
REVIEW_LEVELS = ('confirmed', 'needs_review', 'TODO')
DEFAULT_REVIEW_LEVELS = ('confirmed', 'needs_review')
PROFILE_DIR = Path(__file__).parent / 'gold_profiles'
BOOTSTRAP_SEED = 20261121
MAX_ERROR_SAMPLES = 2000
# DiMB-RE bidirectional relation types (PURE/icl/prompts/desc_guid_1shot_021424.txt:
# "bidirectional relation types, Interacts with, and (Positively/Negatively) Associated with").
DIMBRE_SYMMETRIC = {'INTERACTS_WITH', 'ASSOCIATED_WITH', 'POS_ASSOCIATED_WITH', 'NEG_ASSOCIATED_WITH'}
VERDICT_ORDER = ['SUPPORTED', 'PARTIALLY_SUPPORTED', 'CONTRADICTED', 'UNSUPPORTED',
                 'INSUFFICIENT_EVIDENCE', 'SOURCE_CONFLICT', 'NOT_ASSESSED']
LIMITATIONS = [
    'Mention 对齐依赖节点名称与金标准 mention 的规范化字面相等，同义改写、缩写展开和指代无法对齐，属于上限约束。',
    '指标只覆盖已对齐文档、已映射谓词和端点已对齐的 claim；覆盖率与排除计数单独报告。',
    'BioRED novelty 未评测。',
    'DiMB-RE 文本由 token 以单个空格重建，与原文排版不同。',
]


# --------------------------------------------------------------- utilities

def norm_text(value):
    """NFKC, casefold, collapse whitespace, strip leading/trailing punctuation."""
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    text = re.sub(r'\s+', ' ', text).strip()
    start, end = 0, len(text)
    while start < end and unicodedata.category(text[start]).startswith('P'):
        start += 1
    while end > start and unicodedata.category(text[end - 1]).startswith('P'):
        end -= 1
    return text[start:end].strip()


def prf(tp, n_pred, n_gold):
    precision = tp / n_pred if n_pred else None
    recall = tp / n_gold if n_gold else None
    if tp == 0 and (n_pred or n_gold):
        # Nothing correct: F1 is 0 even when one side is empty (P or R undefined).
        f1 = 0.0
    elif precision is None or recall is None:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return dict(tp=tp, fp=n_pred - tp, fn=n_gold - tp, n_pred=n_pred, n_gold=n_gold,
                precision=precision, recall=recall, f1=f1)


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def spearman(xs, ys):
    """Spearman rank correlation (average ranks for ties); None when undefined."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return dict(rho=None, n=n)

    def ranks(values):
        order = sorted(range(len(values)), key=values.__getitem__)
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                result[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return result

    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx, syy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    if not sxx or not syy:
        return dict(rho=None, n=n)
    return dict(rho=sxy / math.sqrt(sxx * syy), n=n)


# ------------------------------------------------------------ identifiers

NAMESPACES = {
    'mesh': 'MESH', 'msh': 'MESH', 'ncbigene': 'NCBIGene', 'gene': 'NCBIGene', 'geneid': 'NCBIGene',
    'entrez': 'NCBIGene', 'entrezgene': 'NCBIGene', 'ncbi gene': 'NCBIGene', 'ncbitaxon': 'NCBITaxon',
    'taxonomy': 'NCBITaxon', 'taxon': 'NCBITaxon', 'species': 'NCBITaxon', 'ncbi taxonomy': 'NCBITaxon',
    'omim': 'OMIM', 'mim': 'OMIM', 'dbsnp': 'dbSNP', 'cellosaurus': 'Cellosaurus', 'cvcl': 'Cellosaurus',
    'chebi': 'CHEBI', 'tmvar': 'tmVar', 'hgvs': 'tmVar',
}
GOLD_TYPE_NAMESPACE = {'GeneOrGeneProduct': 'NCBIGene', 'OrganismTaxon': 'NCBITaxon'}


def _local(namespace, local):
    local = local.strip()
    if namespace == 'MESH':
        return local.upper()
    if namespace == 'Cellosaurus':
        return 'CVCL_' + re.sub(r'^cvcl[_:]?', '', local, flags=re.I).upper()
    if namespace == 'dbSNP':
        return 'rs' + re.sub(r'^rs', '', local, flags=re.I)
    if namespace in {'NCBIGene', 'NCBITaxon', 'OMIM', 'CHEBI'} and local.isdigit():
        return str(int(local))
    return local


def gold_id(raw, gold_type):
    """Canonical ``NS:LOCAL`` for an identifier as written in BioRED (namespace inferred from type)."""
    raw = str(raw or '').strip()
    if not raw or raw == '-':
        return None
    match = re.match(r'^([A-Za-z][A-Za-z ]*?)\s*:\s*(.+)$', raw)
    if match and match.group(1).casefold() in NAMESPACES:
        ns = NAMESPACES[match.group(1).casefold()]
        return f'{ns}:{_local(ns, match.group(2))}'
    if gold_type in GOLD_TYPE_NAMESPACE:
        ns = GOLD_TYPE_NAMESPACE[gold_type]
        return f'{ns}:{_local(ns, raw)}'
    if re.fullmatch(r'[DC]\d+', raw, flags=re.I):
        return f'MESH:{raw.upper()}'
    if re.fullmatch(r'rs\d+', raw, flags=re.I):
        return f'dbSNP:{_local("dbSNP", raw)}'
    if re.fullmatch(r'CVCL_\w+', raw, flags=re.I):
        return f'Cellosaurus:{_local("Cellosaurus", raw)}'
    if gold_type == 'SequenceVariant':
        return f'tmVar:{raw}'
    return raw


def predicted_id_keys(raw):
    """Comparable keys for a system identifier; bare digits match gene or taxon IDs."""
    raw = str(raw or '').strip()
    if not raw or raw == '-':
        return set()
    match = re.match(r'^([A-Za-z][A-Za-z ]*?)\s*:\s*(.+)$', raw)
    if match and match.group(1).casefold() in NAMESPACES:
        ns = NAMESPACES[match.group(1).casefold()]
        return {f'{ns}:{_local(ns, match.group(2))}'.casefold()}
    if re.fullmatch(r'\d+', raw):
        return {f'NCBIGene:{int(raw)}'.casefold(), f'NCBITaxon:{int(raw)}'.casefold()}
    if re.fullmatch(r'[DC]\d+', raw, flags=re.I):
        return {f'MESH:{raw.upper()}'.casefold()}
    if re.fullmatch(r'rs\d+', raw, flags=re.I):
        return {f'dbSNP:{_local("dbSNP", raw)}'.casefold()}
    if re.fullmatch(r'CVCL_\w+', raw, flags=re.I):
        return {f'Cellosaurus:{_local("Cellosaurus", raw)}'.casefold()}
    return {raw.casefold(), f'tmVar:{raw}'.casefold()}


# --------------------------------------------------------------- gold sets

def validate_gold(kind, payload):
    if kind not in KINDS:
        raise ValueError('GOLD_KIND_INVALID')
    if not isinstance(payload, dict) or payload.get('schema') != GOLD_SCHEMA or payload.get('kind') != kind:
        raise ValueError('GOLD_PAYLOAD_INVALID')
    for key in ('documents', 'entities', 'relations', 'sentences'):
        if not isinstance(payload.get(key), list):
            raise ValueError('GOLD_PAYLOAD_INVALID')
    if not isinstance(payload.get('labels'), dict) or not payload['documents'] or not payload['relations']:
        raise ValueError('GOLD_PAYLOAD_INVALID')
    doc_ids = set()
    for doc in payload['documents']:
        if not isinstance(doc, dict) or not isinstance(doc.get('id'), str) or not isinstance(doc.get('text'), str):
            raise ValueError('GOLD_PAYLOAD_INVALID')
        doc_ids.add(doc['id'])
    if len(doc_ids) != len(payload['documents']):
        raise ValueError('GOLD_DUPLICATE_DOCUMENT')
    for row in payload['entities'] + payload['relations'] + payload['sentences']:
        if not isinstance(row, dict) or row.get('doc_id') not in doc_ids:
            raise ValueError('GOLD_PAYLOAD_INVALID')
    texts = {d['id']: d['text'] for d in payload['documents']}
    for entity in payload['entities']:
        if not isinstance(entity.get('ids', []), list) or texts[entity['doc_id']][entity['start']:entity['end']] != entity['text']:
            raise ValueError('GOLD_ENTITY_OFFSET_INVALID')
    required = ('id1', 'id2', 'type') if kind == 'biored' else ('head_span', 'tail_span', 'type', 'factuality')
    if any(any(k not in r for k in required) for r in payload['relations']):
        raise ValueError('GOLD_PAYLOAD_INVALID')
    return dict(documents=len(payload['documents']), entities=len(payload['entities']),
                relations=len(payload['relations']), sentences=len(payload['sentences']),
                labels=payload['labels'], split=payload.get('split'))


# ---------------------------------------------------------------- profiles

def list_profiles():
    result = []
    for path in sorted(PROFILE_DIR.glob('*.json')):
        profile, profile_hash = load_profile(path.stem)
        todo = [f'{bench}:{rule.get("id")}' for bench, spec in profile['benchmarks'].items()
                for key in ('relation_rules', 'entity_types', 'factuality_rules')
                for rule in spec.get(key, []) if rule.get('review') == 'TODO']
        result.append(dict(name=path.stem, version=profile.get('version'), description=profile.get('description'),
                           hash=profile_hash, benchmarks=sorted(profile['benchmarks']), todo=todo))
    return result


def load_profile(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
        raise ValueError('GOLD_PROFILE_INVALID')
    path = PROFILE_DIR / f'{name}.json'
    if not path.is_file():
        raise ValueError('GOLD_PROFILE_NOT_FOUND')
    profile = json.loads(path.read_text(encoding='utf-8'))
    for spec in profile.get('benchmarks', {}).values():
        for key in ('relation_rules', 'entity_types', 'factuality_rules'):
            for rule in spec.get(key, []):
                if rule.get('review') not in REVIEW_LEVELS:
                    raise ValueError('GOLD_PROFILE_REVIEW_INVALID')
    return profile, digest(profile)


def _matches_when(claim, when, flags):
    for path, expected in (when or {}).items():
        if path.startswith('$'):
            actual = flags.get(path[1:])
        else:
            actual = at(claim, path)
        options = expected if isinstance(expected, list) else [expected]
        if expected == '$absent':
            if actual is not None:
                return False
        elif not any(str(actual).casefold() == str(o).casefold() for o in options if actual is not None):
            return False
    return True


def _truthy_path(claim, paths):
    for path in paths:
        value = at(claim, path)
        if value is True or (isinstance(value, str) and value.casefold() in {'true', 'yes', '1'}):
            return True
    return False


def claim_flags(claim, profile):
    speculative = _truthy_path(claim, profile.get('speculation_fields', []))
    modality = at(claim, 'qualifiers.modality')
    if isinstance(modality, str) and modality.casefold() in {
            v.casefold() for v in profile.get('speculative_modality_values', [])}:
        speculative = True
    return dict(negated=_truthy_path(claim, profile.get('negation_fields', [])), speculative=speculative)


def map_claim(claim, spec, levels, flags):
    """Return (status, rule, label, swap). status: mapped/unsupported/unmapped/excluded_review."""
    predicate = str(claim.get('predicate', ''))
    for rule in spec.get('relation_rules', []):
        if rule.get('predicate') not in {'*', predicate} and str(rule.get('predicate', '')).casefold() != predicate.casefold():
            continue
        if not _matches_when(claim, rule.get('when'), flags):
            continue
        if rule.get('review') not in levels or not rule.get('label'):
            return 'excluded_review', rule, None, False
        return 'mapped', rule, rule['label'], bool(rule.get('swap'))
    if predicate.upper() in {p.upper() for p in spec.get('unsupported_predicates', [])}:
        return 'unsupported', None, None, False
    return 'unmapped', None, None, False


def mapped_types(node, spec, levels):
    by_type = defaultdict(set)
    for rule in spec.get('entity_types', []):
        if rule.get('review') in levels and rule.get('label'):
            by_type[rule['type']].add(rule['label'])
    labels = set()
    for t in node.get('types', []) or []:
        labels |= by_type.get(t, set())
    return labels


# --------------------------------------------------------------- alignment

def align_documents(graph_docs, gold_docs):
    by_external = {d.get('external_id') or d['id']: d['id'] for d in gold_docs}
    by_external.update({d['id']: d['id'] for d in gold_docs})
    titles = defaultdict(list)
    for d in gold_docs:
        if d.get('title'):
            titles[norm_text(d['title'])].append(d['id'])
    gold_ids = {d['id'] for d in gold_docs}
    mapping, methods, conflicts, unaligned = {}, Counter(), [], []
    used = {}
    for doc in graph_docs:
        target, method = None, None
        external = doc.get('external_id')
        if isinstance(external, str) and external in by_external:
            target, method = by_external[external], 'external_id'
        elif doc.get('title') and len(titles.get(norm_text(doc['title']), [])) == 1:
            target, method = titles[norm_text(doc['title'])][0], 'title'
        elif str(doc.get('id')) in gold_ids:
            target, method = str(doc['id']), 'id'
        if target is None:
            unaligned.append(str(doc.get('id')))
        elif target in used:
            conflicts.append(dict(graph_document=str(doc.get('id')), gold_document=target, kept=used[target]))
        else:
            used[target] = str(doc['id'])
            mapping[str(doc['id'])] = target
            methods[method] += 1
    aligned_gold = sorted(mapping.values())
    return mapping, dict(graph_documents=len(graph_docs), gold_documents=len(gold_docs), aligned=len(mapping),
                         coverage=len(mapping) / len(gold_docs) if gold_docs else None,
                         unaligned_graph_documents=len(unaligned), unaligned_gold_documents=len(gold_docs) - len(mapping),
                         by_method=dict(methods), conflicts=conflicts[:50], conflict_count=len(conflicts),
                         aligned_gold_documents_hash=digest(aligned_gold))


def claim_document(claim):
    if claim.get('document_id') is not None:
        return str(claim['document_id'])
    for ref in claim.get('source_refs') or []:
        doc = ref.get('doc_id', ref.get('document_id')) if isinstance(ref, dict) else None
        if doc is not None:
            return str(doc)
    return None


def evidence_quotes(claim):
    quotes = []
    for ref in claim.get('source_refs') or []:
        if isinstance(ref, dict):
            quote = ref.get('quote', ref.get('sentence'))
            if isinstance(quote, str) and quote.strip():
                quotes.append(quote)
    return quotes


def surface_forms(node, profile):
    forms = []
    for field in profile.get('surface_fields', ['label', 'aliases']):
        value = node.get(field)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item.strip():
                forms.append(item)
    return list(dict.fromkeys(forms))


def node_id_values(node, profile):
    values = []
    for field in profile.get('entity_id_fields', ['xrefs']):
        value = node.get(field)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, (str, int)) and str(item).strip():
                values.append(str(item))
    return values


# ------------------------------------------------------------------ BioRED

class _BioredDoc:
    def __init__(self, gold_doc_id, entities, relations):
        self.id_type, self.dictionary, self.mention_text = {}, defaultdict(set), {}
        for e in entities:
            for raw in e.get('ids', []):
                canon = gold_id(raw, e['type'])
                if canon is None:
                    continue
                self.id_type.setdefault(canon, e['type'])
                self.mention_text.setdefault(canon, e['text'])
                key = norm_text(e['text'])
                if key:
                    self.dictionary[key].add(canon)
        self.keys = {c.casefold(): c for c in self.id_type}
        self.pairs, self.typed = set(), set()
        self.relations = []
        for r in relations:
            a = gold_id(r['id1'], self._type_of(r['id1'], entities))
            b = gold_id(r['id2'], self._type_of(r['id2'], entities))
            if a is None or b is None or a == b:
                continue
            pair = frozenset((a, b))
            self.pairs.add(pair)
            self.typed.add((pair, r['type']))
            self.relations.append(dict(r, pair=pair))

    @staticmethod
    def _type_of(raw, entities):
        return next((e['type'] for e in entities if raw in e.get('ids', [])), None)

    def category(self, pair):
        return '–'.join(sorted(self.id_type.get(x, '?') for x in pair))

    def resolve_id(self, node, profile):
        keys = set()
        for value in node_id_values(node, profile):
            keys |= predicted_id_keys(value)
        hits = {self.keys[k] for k in keys if k in self.keys}
        if not keys:
            return None, 'no_identifier'
        if len(hits) == 1:
            return next(iter(hits)), 'resolved'
        return None, 'ambiguous' if hits else 'unresolved'

    def resolve_mention(self, node, profile, allowed_types):
        forms = [norm_text(f) for f in surface_forms(node, profile)]
        hits = set()
        for form in forms:
            hits |= self.dictionary.get(form, set())
        if allowed_types:
            hits = {h for h in hits if self.id_type.get(h) in allowed_types}
        if len(hits) == 1:
            return next(iter(hits)), 'resolved'
        return None, 'ambiguous' if len(hits) > 1 else 'unresolved'


def _bootstrap(doc_counts, rng, rounds):
    """doc_counts: list of dicts {pair:(tp,pred,gold), pair_type:(...)} per aligned doc."""
    results = {key: defaultdict(list) for key in ('pair', 'pair_type')}
    valid = Counter()
    n = len(doc_counts)
    if not n:
        return {}
    for _ in range(rounds):
        sample = [doc_counts[rng.randrange(n)] for _ in range(n)]
        for key in results:
            tp = sum(d[key][0] for d in sample)
            pred = sum(d[key][1] for d in sample)
            gold = sum(d[key][2] for d in sample)
            metric = prf(tp, pred, gold)
            for name in ('precision', 'recall', 'f1'):
                if metric[name] is not None:
                    results[key][name].append(metric[name])
                    valid[(key, name)] += 1
    return {key: {name: dict(low=percentile(values, 0.025), high=percentile(values, 0.975),
                             resamples=valid[(key, name)])
                  for name, values in metrics.items()} for key, metrics in results.items()}


def _macro(per_doc):
    result = {}
    for key in ('pair', 'pair_type'):
        for name in ('precision', 'recall', 'f1'):
            values = [d[key][name] for d in per_doc if d[key][name] is not None]
            result.setdefault(key, {})[name] = dict(value=sum(values) / len(values) if values else None, n=len(values))
    return result


def evaluate_biored(graph, gold, profile, mode, levels, mapping, nodes, bootstrap_rounds):
    spec = profile['benchmarks'].get('biored')
    if spec is None:
        raise ValueError('GOLD_PROFILE_BENCHMARK_MISSING')
    entities, relations = defaultdict(list), defaultdict(list)
    for e in gold['entities']:
        entities[e['doc_id']].append(e)
    for r in gold['relations']:
        relations[r['doc_id']].append(r)
    docs = {gid: _BioredDoc(gid, entities[gid], relations[gid]) for gid in set(mapping.values())}
    counts = Counter()
    rule_hits = Counter()
    endpoint = Counter()
    predicted = defaultdict(lambda: defaultdict(list))   # gold doc -> (pair,label) -> claim ids
    judgements, unaligned_samples, claim_info = {}, [], {}
    for claim in graph['claims']:
        counts['total'] += 1
        doc = claim_document(claim)
        if doc not in mapping:
            counts['outside_aligned_documents'] += 1
            continue
        counts['in_aligned_documents'] += 1
        flags = claim_flags(claim, profile)
        if flags['negated']:
            counts['negated_excluded'] += 1
            continue
        if flags['speculative']:
            counts['speculative_excluded'] += 1
            continue
        status, rule, label, _ = map_claim(claim, spec, levels, flags)
        if rule is not None:
            rule_hits[f'{rule.get("id")}:{status}'] += 1
        if status != 'mapped':
            counts[f'{status}_predicate'] += 1
            continue
        counts['considered'] += 1
        gdoc = docs[mapping[doc]]
        ends = []
        for role in ('subject', 'object'):
            endpoint_value = claim.get(role) or {}
            node = nodes.get(endpoint_value.get('value'), {}) if endpoint_value.get('kind') == 'entity' else {}
            if mode == 'id':
                resolved, state = gdoc.resolve_id(node, profile)
            else:
                allowed = mapped_types(node, spec, levels) if profile.get('mention_type_filter') else set()
                resolved, state = gdoc.resolve_mention(node, profile, allowed)
            endpoint[state] += 1
            ends.append(resolved)
        if None in ends:
            counts['endpoint_unaligned'] += 1
            if len(unaligned_samples) < MAX_ERROR_SAMPLES:
                unaligned_samples.append(dict(claim_id=claim['id'], doc_id=mapping[doc], predicate=claim.get('predicate'),
                                              label=label, subject=_label(claim, 'subject', nodes),
                                              object=_label(claim, 'object', nodes), quotes=evidence_quotes(claim)[:3]))
            continue
        if ends[0] == ends[1]:
            counts['self_pair_excluded'] += 1
            continue
        counts['endpoint_aligned'] += 1
        claim_info[claim['id']] = dict(claim_id=claim['id'], predicate=claim.get('predicate'),
                                       subject=_label(claim, 'subject', nodes), object=_label(claim, 'object', nodes),
                                       quotes=evidence_quotes(claim)[:3])
        pair = frozenset(ends)
        predicted[mapping[doc]][(pair, label)].append(claim['id'])
        judgements[claim['id']] = dict(doc_id=mapping[doc], pair=pair, label=label,
                                       tp_pair=pair in gdoc.pairs, tp_pair_type=(pair, label) in gdoc.typed)

    totals = {k: Counter() for k in ('pair', 'pair_type')}
    by_type, by_category = defaultdict(Counter), defaultdict(Counter)
    per_doc, doc_counts, fp, fn = [], [], [], []
    for gid in sorted(docs):
        gdoc = docs[gid]
        typed_pred = set(predicted[gid])
        pairs_pred = {p for p, _ in typed_pred}
        tp_pair = len(pairs_pred & gdoc.pairs)
        tp_typed = len(typed_pred & gdoc.typed)
        row = dict(doc_id=gid, pair=prf(tp_pair, len(pairs_pred), len(gdoc.pairs)),
                   pair_type=prf(tp_typed, len(typed_pred), len(gdoc.typed)))
        per_doc.append(row)
        doc_counts.append(dict(pair=(tp_pair, len(pairs_pred), len(gdoc.pairs)),
                               pair_type=(tp_typed, len(typed_pred), len(gdoc.typed))))
        for key, (tp, pred, gold_n) in doc_counts[-1].items():
            totals[key].update(tp=tp, pred=pred, gold=gold_n)
        for pair, label in typed_pred:
            hit = (pair, label) in gdoc.typed
            by_type[label].update(pred=1, tp=int(hit))
            category = gdoc.category(pair)
            by_category[category].update(pred_type=1, tp_type=int(hit))
            if not hit and len(fp) < MAX_ERROR_SAMPLES:
                gold_same_pair = [r['type'] for r in gdoc.relations if r['pair'] == pair]
                fp.append(dict(doc_id=gid, label=label, ids=sorted(pair),
                               names=[gdoc.mention_text.get(x) for x in sorted(pair)],
                               claim_ids=predicted[gid][(pair, label)][:10],
                               claims=[claim_info[c] for c in predicted[gid][(pair, label)][:3]],
                               gold_types_for_pair=gold_same_pair,
                               reason='wrong_type' if gold_same_pair else 'pair_not_in_gold'))
        for pair in pairs_pred:
            by_category[gdoc.category(pair)].update(pred_pair=1, tp_pair=int(pair in gdoc.pairs))
        for pair in gdoc.pairs:
            by_category[gdoc.category(pair)].update(gold_pair=1)
        for pair, label in gdoc.typed:
            by_type[label].update(gold=1)
            by_category[gdoc.category(pair)].update(gold_type=1)
            if (pair, label) not in typed_pred and len(fn) < MAX_ERROR_SAMPLES:
                fn.append(dict(doc_id=gid, label=label, ids=sorted(pair),
                               names=[gdoc.mention_text.get(x) for x in sorted(pair)],
                               predicted_types_for_pair=sorted(l for p, l in typed_pred if p == pair)))
    micro = {key: prf(c['tp'], c['pred'], c['gold']) for key, c in totals.items()}
    per_type = {label: prf(c['tp'], c['pred'], c['gold']) for label, c in sorted(by_type.items())}
    per_category = {cat: dict(pair=prf(c['tp_pair'], c['pred_pair'], c['gold_pair']),
                              pair_type=prf(c['tp_type'], c['pred_type'], c['gold_type']))
                    for cat, c in sorted(by_category.items())}
    aligned_endpoints = endpoint['resolved']
    total_endpoints = sum(endpoint.values())
    metrics = dict(micro=micro, macro=_macro(per_doc), per_type=per_type, per_entity_pair=per_category,
                   bootstrap=dict(rounds=bootstrap_rounds, seed=BOOTSTRAP_SEED, level=0.95,
                                  intervals=_bootstrap(doc_counts, random.Random(BOOTSTRAP_SEED), bootstrap_rounds)),
                   endpoint_alignment=dict(resolved=aligned_endpoints, total=total_endpoints,
                                           coverage=aligned_endpoints / total_endpoints if total_endpoints else None,
                                           ambiguous=endpoint['ambiguous'], unresolved=endpoint['unresolved'],
                                           no_identifier=endpoint['no_identifier']),
                   unsupported_gold_labels=sorted(spec.get('unsupported_labels', [])))
    return dict(claims=dict(counts), rule_hits=dict(rule_hits), metrics=metrics, per_document=per_doc,
                judgements={cid: dict(doc_id=j['doc_id'], label=j['label'], tp_pair=j['tp_pair'],
                                      tp_pair_type=j['tp_pair_type']) for cid, j in judgements.items()},
                errors=dict(fp=fp, fn=fn, endpoint_unaligned=unaligned_samples))


def _label(claim, role, nodes):
    value = claim.get(role) or {}
    if value.get('kind') == 'entity':
        return nodes.get(value.get('value'), {}).get('label', value.get('value'))
    return value.get('value')


# ----------------------------------------------------------------- DiMB-RE

def _locate(quote, text):
    """Unique span of quote in text: exact, then whitespace-flexible."""
    if not quote:
        return None, 'no_quote'
    first = text.find(quote)
    if first >= 0:
        return ((first, first + len(quote)), 'located') if text.find(quote, first + 1) < 0 else (None, 'ambiguous')
    words = quote.split()
    if not words:
        return None, 'no_quote'
    pattern = re.compile(r'\s*'.join(re.escape(w) for w in words))
    hits = [m.span() for m in pattern.finditer(text)]
    if len(hits) == 1:
        return hits[0], 'located'
    return None, 'ambiguous' if hits else 'not_found'


def _occurrences(form, text, window):
    words = form.split()
    if not words:
        return []
    pattern = re.compile(r'(?<!\w)' + r'\s*'.join(re.escape(w) for w in words) + r'(?!\w)', re.I)
    start, end = window
    return [(m.start() + start, m.end() + start) for m in pattern.finditer(text[start:end])]


def evaluate_dimbre(graph, gold, profile, levels, mapping, nodes):
    spec = profile['benchmarks'].get('dimbre')
    if spec is None:
        raise ValueError('GOLD_PROFILE_BENCHMARK_MISSING')
    texts = {d['id']: d['text'] for d in gold['documents']}
    sentences = defaultdict(list)
    for s in gold['sentences']:
        sentences[s['doc_id']].append(s)
    relations = defaultdict(list)
    for index, r in enumerate(gold['relations']):
        relations[r['doc_id']].append(dict(r, index=index))
    graph_blocks = {(str(d['id']), str(b.get('id'))): b for d in graph['documents'] for b in d.get('blocks', [])}
    counts, rule_hits, locate_states = Counter(), Counter(), Counter()
    located_claims = defaultdict(list)
    unlocated_samples = []
    for claim in graph['claims']:
        counts['total'] += 1
        doc = claim_document(claim)
        if doc not in mapping:
            counts['outside_aligned_documents'] += 1
            continue
        counts['in_aligned_documents'] += 1
        flags = claim_flags(claim, profile)
        status, rule, label, swap = map_claim(claim, spec, levels, flags)
        if rule is not None:
            rule_hits[f'{rule.get("id")}:{status}'] += 1
        if status != 'mapped':
            counts[f'{status}_predicate'] += 1
            continue
        counts['considered'] += 1
        gid = mapping[doc]
        text = texts[gid]
        spans, state = [], 'no_quote'
        for ref in claim.get('source_refs') or []:
            if not isinstance(ref, dict):
                continue
            quote = ref.get('quote', ref.get('sentence'))
            block = graph_blocks.get((str(ref.get('doc_id', ref.get('document_id'))),
                                      str(ref.get('block_id', ref.get('chunk_id')))))
            start, end = ref.get('start'), ref.get('end')
            offset = ref.get('offset') or {}
            if start is None:
                start, end = offset.get('start'), offset.get('end')
            base = block.get('source_offset') if isinstance(block, dict) else None
            if isinstance(base, int) and isinstance(start, int) and isinstance(end, int) \
                    and isinstance(quote, str) and text[base + start:base + end] == quote:
                spans.append((base + start, base + end))
                state = 'located'
                continue
            span, found = _locate(quote, text) if isinstance(quote, str) else (None, 'no_quote')
            if span:
                spans.append(span)
                state = 'located'
            elif state != 'located':
                state = found
        locate_states[state] += 1
        if not spans:
            if len(unlocated_samples) < MAX_ERROR_SAMPLES:
                unlocated_samples.append(dict(claim_id=claim['id'], doc_id=gid, reason=state,
                                              subject=_label(claim, 'subject', nodes),
                                              object=_label(claim, 'object', nodes), quotes=evidence_quotes(claim)[:3]))
            continue
        sent_ids = {s['index'] for s in sentences[gid] for a, b in spans if s['start'] < b and s['end'] > a}
        roles = ('object', 'subject') if swap else ('subject', 'object')
        forms = []
        for role in roles:
            value = claim.get(role) or {}
            node = nodes.get(value.get('value'), {}) if value.get('kind') == 'entity' else {'label': value.get('value')}
            forms.append(surface_forms(node, profile) or [str(value.get('value', ''))])
        predicted_factuality = _factuality(claim, spec, levels, flags)
        located_claims[gid].append(dict(claim_id=claim['id'], label=label, sentences=sent_ids, forms=forms,
                                        factuality=predicted_factuality, quotes=evidence_quotes(claim)[:3]))
    counts['located'] = sum(len(v) for v in located_claims.values())
    result = dict(claims=dict(counts), rule_hits=dict(rule_hits), location=dict(locate_states), metrics={},
                  errors={}, judgements={})
    for level in ('strict', 'relaxed'):
        level_metrics, errors, judgements = _dimbre_level(level, mapping, texts, sentences, relations, located_claims)
        result['metrics'][level] = level_metrics
        result['errors'][level] = errors
        result['judgements'][level] = judgements
    result['errors']['unlocated'] = unlocated_samples
    result['metrics']['location_coverage'] = dict(
        located=counts['located'], considered=counts['considered'],
        coverage=counts['located'] / counts['considered'] if counts['considered'] else None)
    result['metrics']['unsupported_gold_labels'] = sorted(spec.get('unsupported_labels', []))
    return result


def _factuality(claim, spec, levels, flags):
    for rule in spec.get('factuality_rules', []):
        if _matches_when(claim, rule.get('when'), flags):
            return rule['label'] if rule.get('review') in levels else None
    return None


def _endpoint_match(level, forms, gold_text, gold_span, text, window):
    if level == 'strict':
        return any(norm_text(f) == norm_text(gold_text) for f in forms)
    if any(norm_text(f) == norm_text(gold_text) for f in forms):
        return True
    a, b = gold_span
    return any(s < b and e > a for f in forms for s, e in _occurrences(f, text, window))


def _dimbre_level(level, mapping, texts, sentences, relations, located_claims):
    tp_typed = tp_pair = n_pred = duplicates = 0
    by_type = defaultdict(Counter)
    fact_total = fact_correct = 0
    fact_confusion = defaultdict(Counter)
    fp, fn, judgements = [], [], {}
    n_gold = 0
    for gid in sorted(set(mapping.values())):
        text = texts[gid]
        bounds = {s['index']: (s['start'], s['end']) for s in sentences[gid]}
        golds = relations[gid]
        n_gold += len(golds)
        for g in golds:
            by_type[g['type']]['gold'] += 1
        matched_typed, matched_pair = set(), set()
        for c in located_claims.get(gid, []):
            candidates = []
            for g in golds:
                g_sents = set(g.get('sentence_indices') or [g['sentence_index']])
                if not (c['sentences'] & g_sents):
                    continue
                window_ids = c['sentences'] | g_sents
                window = (min(bounds[i][0] for i in window_ids), max(bounds[i][1] for i in window_ids))
                forward = (_endpoint_match(level, c['forms'][0], g['head_text'], g['head_span'], text, window) and
                           _endpoint_match(level, c['forms'][1], g['tail_text'], g['tail_span'], text, window))
                backward = g['type'] in DIMBRE_SYMMETRIC and (
                    _endpoint_match(level, c['forms'][1], g['head_text'], g['head_span'], text, window) and
                    _endpoint_match(level, c['forms'][0], g['tail_text'], g['tail_span'], text, window))
                if forward or backward:
                    candidates.append(g)
            typed = [g for g in candidates if g['type'] == c['label']]
            free_typed = [g for g in typed if g['index'] not in matched_typed]
            if typed and not free_typed:
                duplicates += 1
                judgements[c['claim_id']] = dict(doc_id=gid, label=c['label'], tp=None, duplicate=True)
                continue
            n_pred += 1
            by_type[c['label']]['pred'] += 1
            free_pair = [g for g in candidates if g['index'] not in matched_pair]
            if free_pair:
                matched_pair.add(free_pair[0]['index'])
                tp_pair += 1
            if free_typed:
                g = free_typed[0]
                matched_typed.add(g['index'])
                tp_typed += 1
                by_type[c['label']]['tp'] += 1
                if c['factuality'] is not None:
                    fact_total += 1
                    fact_correct += int(c['factuality'] == g['factuality'])
                    fact_confusion[g['factuality']][c['factuality']] += 1
                judgements[c['claim_id']] = dict(doc_id=gid, label=c['label'], tp=True)
            else:
                judgements[c['claim_id']] = dict(doc_id=gid, label=c['label'], tp=False)
                if len(fp) < MAX_ERROR_SAMPLES:
                    fp.append(dict(doc_id=gid, claim_id=c['claim_id'], label=c['label'], forms=c['forms'],
                                   quotes=c['quotes'], gold_types_same_endpoints=[g['type'] for g in candidates],
                                   reason='wrong_type' if candidates else 'no_gold_relation'))
        for g in golds:
            if g['index'] not in matched_typed and len(fn) < MAX_ERROR_SAMPLES:
                s0, s1 = bounds[g['sentence_index']]
                fn.append(dict(doc_id=gid, label=g['type'], head=g['head_text'], tail=g['tail_text'],
                               factuality=g['factuality'], sentence=text[s0:s1]))
    per_type = {label: prf(c['tp'], c['pred'], c['gold']) for label, c in sorted(by_type.items())}
    metrics = dict(relation_type=prf(tp_typed, n_pred, n_gold), relation_pair=prf(tp_pair, n_pred, n_gold),
                   per_type=per_type, duplicates_ignored=duplicates,
                   factuality=dict(accuracy=fact_correct / fact_total if fact_total else None, n=fact_total,
                                   confusion={k: dict(v) for k, v in fact_confusion.items()}))
    return metrics, dict(fp=fp, fn=fn), judgements


# ------------------------------------------------------------- calibration

def calibrate(graph, qc_report, judgements, doc_metrics, mapping):
    """Associate per-claim gold judgements with QC verdicts; observational only."""
    if not qc_report or not isinstance(qc_report.get('assessments'), list):
        return None
    verdicts = {str(a.get('claim_id')): a.get('verdict') for a in qc_report['assessments']}
    table = {k: Counter() for k in ('TP', 'FP')}
    for cid, j in judgements.items():
        if j.get('tp') is None:
            continue
        table['TP' if j['tp'] else 'FP'][verdicts.get(cid) or 'NOT_ASSESSED'] += 1

    def rate(row):
        assessed = sum(v for k, v in table[row].items() if k != 'NOT_ASSESSED')
        return dict(value=table[row]['SUPPORTED'] / assessed if assessed else None, n=assessed)

    claim_doc = {c['id']: claim_document(c) for c in graph['claims']}
    qc_correct = defaultdict(Counter)
    for cid, verdict in verdicts.items():
        doc = mapping.get(claim_doc.get(cid))
        if doc:
            qc_correct[doc].update(total=1, supported=int(verdict == 'SUPPORTED'))
    candidates = {c.get('id'): c for c in qc_report.get('candidates', []) if isinstance(c, dict)}
    qc_complete = defaultdict(Counter)
    for match in qc_report.get('matches', []):
        candidate = candidates.get(match.get('candidate_id'))
        evidence = (candidate or {}).get('evidence') or []
        doc = mapping.get(str(evidence[0].get('doc_id'))) if evidence and isinstance(evidence[0], dict) else None
        if doc and match.get('status') in {'matched', 'misrepresented', 'missing'}:
            qc_complete[doc].update(total=1, matched=int(match['status'] == 'matched'))
    docs = sorted(doc_metrics)
    correctness = [qc_correct[d]['supported'] / qc_correct[d]['total'] if qc_correct[d]['total'] else None for d in docs]
    completeness = [qc_complete[d]['matched'] / qc_complete[d]['total'] if qc_complete[d]['total'] else None for d in docs]
    return dict(
        confusion={row: {v: table[row].get(v, 0) for v in VERDICT_ORDER} for row in table},
        p_supported_given_tp=rate('TP'), p_supported_given_fp=rate('FP'),
        spearman_correctness_vs_gold_precision=spearman(correctness, [doc_metrics[d]['precision'] for d in docs]),
        spearman_completeness_vs_gold_recall=spearman(completeness, [doc_metrics[d]['recall'] for d in docs]),
        note='仅描述本次样本中观察到的关联（附 n），不代表因果关系，也不外推到其他语料或系统。')


# -------------------------------------------------------------------- main

def evaluate(graph, gold, kind, profile_name, modes=('id', 'mention'), *, review_levels=DEFAULT_REVIEW_LEVELS,
             qc_report=None, qc_run_id=None, bootstrap_rounds=1000, gold_set=None):
    """Return (report, errors). Inputs are not modified."""
    if kind != gold.get('kind'):
        raise ValueError('GOLD_KIND_MISMATCH')
    levels = tuple(level for level in REVIEW_LEVELS if level in set(review_levels))
    if not levels or any(level not in REVIEW_LEVELS for level in review_levels):
        raise ValueError('GOLD_REVIEW_LEVELS_INVALID')
    if not isinstance(bootstrap_rounds, int) or isinstance(bootstrap_rounds, bool) or not 0 <= bootstrap_rounds <= 10000:
        raise ValueError('GOLD_BOOTSTRAP_INVALID')
    profile, profile_hash = load_profile(profile_name)
    nodes = {n['id']: n for n in graph['nodes']}
    mapping, alignment = align_documents(graph['documents'], gold['documents'])
    report = dict(version=GOLD_EVAL_VERSION, kind=kind, gold_name=gold.get('name'), gold_split=gold.get('split'),
                  gold_hash=digest(gold), profile=dict(name=profile_name, version=profile.get('version'), hash=profile_hash),
                  review_levels=list(levels), graph_name=graph.get('name'), snapshot_hash=graph.get('snapshot_hash'),
                  corpus_hash=digest(graph['documents']), policy_hash=digest(graph['policy']),
                  alignment=alignment, limitations=list(LIMITATIONS), gold_set_id=gold_set)
    errors = {}
    if kind == 'biored':
        modes = [m for m in ('id', 'mention') if m in set(modes)]
        if not modes or any(m not in MODES for m in modes):
            raise ValueError('GOLD_MODE_INVALID')
        report['modes'] = modes
        report['results'] = {}
        for mode in modes:
            result = evaluate_biored(graph, gold, profile, mode, levels, mapping, nodes, bootstrap_rounds)
            errors[mode] = result.pop('errors')
            judgements = result.pop('judgements')
            doc_metrics = {row['doc_id']: row['pair_type'] for row in result['per_document']}
            result['calibration'] = calibrate(
                graph, qc_report, {cid: dict(j, tp=j['tp_pair_type']) for cid, j in judgements.items()},
                doc_metrics, mapping) if qc_report else None
            report['results'][mode] = result
    else:
        report['modes'] = ['strict', 'relaxed']
        result = evaluate_dimbre(graph, gold, profile, levels, mapping, nodes)
        errors = result.pop('errors')
        judgements = result.pop('judgements')
        result['calibration'] = {}
        for level in ('strict', 'relaxed'):
            per_doc = defaultdict(Counter)
            for j in judgements[level].values():
                if j.get('tp') is not None:
                    per_doc[j['doc_id']].update(pred=1, tp=int(j['tp']))
            gold_per_doc = Counter(r['doc_id'] for r in gold['relations'] if r['doc_id'] in set(mapping.values()))
            doc_metrics = {d: prf(per_doc[d]['tp'], per_doc[d]['pred'], gold_per_doc[d]) for d in set(mapping.values())}
            result['calibration'][level] = calibrate(graph, qc_report, judgements[level], doc_metrics, mapping) if qc_report else None
        report['results'] = {'span': result}
    report['qc_run_id'] = qc_run_id
    report['calibration_available'] = bool(qc_report)
    return report, errors


# ------------------------------------------------- automatic review (all gold sets)

# The metric that enters the six-dimension scores.  Mention alignment works for every system (names are
# always present, identifiers are not), so systems are compared on the same footing; the id mode and the
# strict level are still computed and reported.
PRIMARY = {'biored': ('mention', 'pair_type'), 'dimbre': ('relaxed', 'relation_type')}
SYSTEM_PROFILES = {'pubtator3': 'pubtator3', 'pubtator3+llm': 'pubtator3', 'neo4j llm graph builder': 'neo4j-builder',
                   'litkg': 'litkg'}


def resolve_profile(graph):
    """Mapping profile for a graph: policy.gold_profile, else the producing system, else LitKG."""
    requested = (graph.get('policy') or {}).get('gold_profile')
    if isinstance(requested, str) and requested.strip():
        return requested.strip(), 'policy.gold_profile'
    metadata = graph.get('metadata') or {}
    system = str(metadata.get('system') or '').strip().casefold()
    if system in SYSTEM_PROFILES:
        return SYSTEM_PROFILES[system], 'metadata.system'
    if str(metadata.get('source') or '').strip().casefold() == 'litkg':
        return 'litkg', 'metadata.source'
    return 'litkg', 'default'


def _calibration_summary(calibration):
    if not calibration:
        return None
    keys = ('confusion', 'p_supported_given_tp', 'p_supported_given_fp',
            'spearman_correctness_vs_gold_precision', 'spearman_completeness_vs_gold_recall')
    return {key: calibration[key] for key in keys}


def _headline(gold_set_id, name, report):
    """Compact per-gold-set entry for the review report; full details stay in gold_evals."""
    kind = report['kind']
    mode, metric = PRIMARY[kind]
    entry = dict(gold_set_id=gold_set_id, name=name, kind=kind, split=report.get('gold_split'),
                 gold_hash=report['gold_hash'],
                 alignment={k: report['alignment'][k] for k in ('graph_documents', 'gold_documents', 'aligned', 'coverage',
                                                                'aligned_gold_documents_hash', 'by_method', 'conflict_count')},
                 primary_mode=mode, primary_metric=metric, modes={}, calibration={})
    if kind == 'biored':
        for name_, result in report['results'].items():
            m = result['metrics']
            intervals = (m.get('bootstrap') or {}).get('intervals') or {}
            entry['modes'][name_] = dict(pair=m['micro']['pair'], pair_type=m['micro']['pair_type'],
                                         f1_ci=(intervals.get('pair_type') or {}).get('f1'),
                                         endpoint_alignment=m['endpoint_alignment'],
                                         judged_claims=result['claims'].get('endpoint_aligned', 0), claims=result['claims'])
            entry['calibration'][name_] = _calibration_summary(result.get('calibration'))
    else:
        span = report['results']['span']
        for level in ('strict', 'relaxed'):
            m = span['metrics'][level]
            entry['modes'][level] = dict(relation_type=m['relation_type'], relation_pair=m['relation_pair'],
                                         factuality=dict(accuracy=m['factuality']['accuracy'], n=m['factuality']['n']),
                                         location_coverage=span['metrics']['location_coverage'],
                                         judged_claims=span['claims'].get('located', 0), claims=span['claims'])
            entry['calibration'][level] = _calibration_summary((span.get('calibration') or {}).get(level))
    primary = entry['modes'][mode]
    entry['primary'] = primary[metric]
    entry['judged_claims'] = primary['judged_claims']
    return entry


def review_all(graph, gold_sets, *, qc_report=None, qc_run_id=None, trigger='review', bootstrap_rounds=1000,
               review_levels=DEFAULT_REVIEW_LEVELS):
    """Evaluate a graph against every gold set its documents align with.

    gold_sets: iterable of (gold_set_id, kind, name, payload).  Returns (summary, evaluations): the summary
    goes into the review report, evaluations[i] = (gold_set_id, report, errors) belongs to summary['sets'][i]
    and is stored in full.  Gold sets of one kind that share documents are pooled once (the one with more
    aligned documents counts), so overlapping splits never count a document twice.
    """
    gold_sets = sorted(gold_sets, key=lambda s: (str(s[2]), str(s[0])))
    profile_name, profile_source = resolve_profile(graph)
    summary = dict(version=GOLD_EVAL_VERSION, trigger=trigger, profile=dict(name=profile_name, source=profile_source),
                   review_levels=list(review_levels), primary={k: dict(mode=m, metric=x) for k, (m, x) in PRIMARY.items()},
                   available_sets=len(gold_sets), sets=[], skipped=[], pooled=None,
                   status='no_gold_sets' if not gold_sets else 'not_applicable')
    if not gold_sets:
        return summary, []
    try:
        _, profile_hash = load_profile(profile_name)
    except ValueError as exc:
        summary.update(status='failed', code=str(exc))
        return summary, []
    summary['profile']['hash'] = profile_hash
    evaluations, gold_docs, graph_docs = [], [], []
    for gold_set_id, kind, name, payload in gold_sets:
        mapping, _ = align_documents(graph['documents'], payload.get('documents') or [])
        if not mapping:
            summary['skipped'].append(dict(gold_set_id=gold_set_id, name=name, kind=kind, reason='no_aligned_documents'))
            continue
        try:
            report, errors = evaluate(graph, payload, kind, profile_name, ('id', 'mention'), review_levels=review_levels,
                                      qc_report=qc_report, qc_run_id=qc_run_id, bootstrap_rounds=bootstrap_rounds,
                                      gold_set=gold_set_id)
        except ValueError as exc:
            summary['skipped'].append(dict(gold_set_id=gold_set_id, name=name, kind=kind, reason=str(exc)))
            continue
        report['trigger'] = trigger
        summary['sets'].append(_headline(gold_set_id, name, report))
        evaluations.append((gold_set_id, report, errors))
        gold_docs.append(set(mapping.values()))
        graph_docs.append(set(mapping))
    if not summary['sets']:
        return summary, evaluations
    used, counted_docs, totals = defaultdict(set), set(), Counter()
    order = sorted(range(len(summary['sets'])), key=lambda i: (-summary['sets'][i]['alignment']['aligned'], summary['sets'][i]['name']))
    for index in order:
        entry = summary['sets'][index]
        overlap = used[entry['kind']] & gold_docs[index]
        if overlap:
            entry.update(pooled=False, pool_note=f'与已计入评分的同类金标准集共有 {len(overlap)} 篇文献，不重复计入')
            continue
        entry['pooled'] = True
        used[entry['kind']] |= gold_docs[index]
        counted_docs |= graph_docs[index]
        primary = entry['primary']
        totals.update(tp=primary['tp'], n_pred=primary['n_pred'], n_gold=primary['n_gold'], judged=entry['judged_claims'])
    claims, documents = len(graph['claims']), len(graph['documents'])
    pooled = prf(totals['tp'], totals['n_pred'], totals['n_gold'])
    judged = min(totals['judged'], claims)
    pooled.update(sets=[summary['sets'][i]['name'] for i in order if summary['sets'][i]['pooled']],
                  judged_claims=judged, total_claims=claims, claim_coverage=judged / claims if claims else 0.0,
                  aligned_graph_documents=len(counted_docs), graph_documents=documents,
                  document_coverage=len(counted_docs) / documents if documents else 0.0)
    summary.update(status='applied', pooled=pooled)
    return summary, evaluations
