"""PubTator3 baselines.

* ``native``: PubTator3 free-text NER on the shared input text, plus document-
  level precomputed relations (BioC-JSON export) for documents with a PMID.
* ``llm``: the same PubTator entities, relations extracted by the LitKG LLM
  inside text windows (entities and relations decoupled, MedKGent style).

Endpoints, limits and observed behaviour are documented in
``docs/pubtator3_api_notes.md``.
"""
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..cache import Cache
from ..common import digest, load_json, ontology, sha256_text, type_allowed
from ..graph import GraphBuilder
from ..http import Http, HttpError
from ..llm import LLMError
from ..text_prep import build, sentences

SUBMIT_URL = 'https://www.ncbi.nlm.nih.gov/CBBresearch/Lu/Demo/RESTful/request.cgi'
RETRIEVE_URL = 'https://www.ncbi.nlm.nih.gov/CBBresearch/Lu/Demo/RESTful/retrieve.cgi'
EXPORT_URL = 'https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson'
ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / 'prompts' / 'pubtator3_llm_relations.md'


class PubTatorError(RuntimeError):
    pass


# ------------------------------------------------------------------ parsing

def parse_pubtator(raw):
    """PubTator text format -> (title text, mentions, relation rows)."""
    title, mentions, relations = None, [], []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = re.match(r'^[^|\t]*\|t\|(.*)$', line)
        if m:
            title = m.group(1)
            continue
        if re.match(r'^[^|\t]*\|a\|', line):
            continue
        parts = line.split('\t')
        if len(parts) >= 5 and parts[1].isdigit() and parts[2].isdigit():
            mentions.append(dict(start=int(parts[1]), end=int(parts[2]), text=parts[3], type=parts[4],
                                 identifier=parts[5] if len(parts) > 5 else ''))
        elif len(parts) >= 4:
            relations.append(dict(type=parts[1], id1=parts[2], id2=parts[3]))
    return title, mentions, relations


def xrefs_for(ptype, identifier):
    """Namespaced identifiers for a PubTator concept ID (bare numbers get NCBI namespaces)."""
    ident = (identifier or '').strip()
    if not ident or ident == '-':
        return []
    if ptype in {'Variant', 'Mutation', 'DNAMutation', 'ProteinMutation', 'SNP'}:
        refs = [f'dbSNP:rs{n}' for n in re.findall(r'RS#:\s*(\d+)', ident)]
        refs += [f'dbSNP:{x}' for x in re.findall(r'\b(rs\d+)\b', ident) if f'dbSNP:{x}' not in refs]
        return refs + [ident if ident.startswith(('tmVar:', 'HGVS:')) else f'tmVar:{ident}']
    result = []
    for part in re.split(r'[;,]', ident):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and ptype == 'Gene':
            result.append(f'NCBIGene:{part}')
        elif part.isdigit() and ptype == 'Species':
            result.append(f'NCBITaxon:{part}')
        else:
            result.append(part)
    return result


def node_id_for(ptype, identifier, text):
    refs = xrefs_for(ptype, identifier)
    if refs:
        return 'p3:' + ';'.join(refs), refs
    return f'p3:unnormalized:{ptype}:{sha256_text(text.casefold())[:12]}', []


# ------------------------------------------------------------------ client

class PubTatorClient:
    def __init__(self, cache_root, cfg, http=None, sleep=time.sleep):
        self.cfg = cfg
        self.http = http or Http(per_second=min(3, cfg.get('requests_per_second', 3)))
        self.results = Cache(cache_root, 'pubtator3-freetext')
        self.pending = Cache(cache_root, 'pubtator3-pending')
        self.exports = Cache(cache_root, 'pubtator3-export')
        self.sleep = sleep
        self.stats = Counter()

    def annotate(self, text, bioconcept):
        key = sha256_text(f'{bioconcept}\0{text}')
        cached = self.results.get(key)
        if cached is not None:
            self.stats['freetext_cache_hits'] += 1
            return cached['raw']
        session = (self.pending.get(key) or {}).get('id')
        if not session:
            response = self.http.request('POST', SUBMIT_URL, data={'text': text, 'bioconcept': bioconcept})
            session = response.json()['id']
            self.pending.put(key, {'id': session})
            self.stats['freetext_submitted'] += 1
        waited = 0
        while True:
            try:
                response = self.http.request('POST', RETRIEVE_URL, data={'id': session}, ok=(200, 404),
                                             retry_status={429, 500, 502, 503, 504})
            except HttpError as exc:
                # Observed: HTTP 400 for sessions of named bioconcepts. Treat it as pending for a
                # bounded time (max_wait_400) in case it only means "still processing".
                if exc.status == 400 and waited < self.cfg.get('max_wait_400', 300):
                    self.stats['freetext_http400_polls'] += 1
                    self.sleep(self.cfg.get('poll_interval', 15))
                    waited += self.cfg.get('poll_interval', 15)
                    continue
                self.pending.put(key, {})   # rejected session: resubmit on the next run
                self.stats[f'freetext_rejected_{bioconcept}'] += 1
                raise
            body = response.text
            if response.status_code == 200 and not body.lstrip().startswith('<'):
                self.results.put(key, {'raw': body, 'bioconcept': bioconcept})
                self.stats['freetext_retrieved'] += 1
                return body
            if response.status_code == 200:
                # HTML error page: session expired or rejected; resubmit next run.
                self.pending.put(key, {})
                raise PubTatorError('free-text session returned an HTML error page')
            if waited >= self.cfg.get('max_wait', 1800):
                raise PubTatorError('free-text result not ready before max_wait')
            self.sleep(self.cfg.get('poll_interval', 15))
            waited += self.cfg.get('poll_interval', 15)

    def export(self, pmids):
        result, missing = {}, []
        for pmid in pmids:
            cached = self.exports.get(sha256_text(f'pmid:{pmid}'))
            if cached is not None:
                result[pmid] = cached
            else:
                missing.append(pmid)
        for start in range(0, len(missing), 20):
            batch = missing[start:start + 20]
            response = self.http.request('GET', EXPORT_URL, params={'pmids': ','.join(batch)})
            docs = response.json()
            docs = docs.get('PubTator3', []) if isinstance(docs, dict) else docs
            found = {str(d.get('pmid') or d.get('id')): d for d in docs}
            for pmid in batch:
                value = found.get(pmid, {})
                self.exports.put(sha256_text(f'pmid:{pmid}'), value)
                result[pmid] = value
            self.stats['export_requests'] += 1
        return result


# --------------------------------------------------------------- NER per doc

def pieces(text, max_chars):
    """Split text at sentence boundaries into pieces of at most max_chars."""
    out, current = [], None
    for start, end in sentences(text) or [(0, len(text))]:
        while end - start > max_chars:
            cut = text.rfind(' ', start, start + max_chars)
            cut = cut if cut > start else start + max_chars
            if current:
                out.append(current)
                current = None
            out.append((start, cut))
            start = cut
            while start < end and text[start].isspace():
                start += 1
        if current and end - current[0] <= max_chars:
            current = (current[0], end)
        else:
            if current:
                out.append(current)
            current = (start, end)
    if current:
        out.append(current)
    return out


def ner_document(doc_text, client, cfg, stats):
    mentions, seen = [], set()
    for start, end in pieces(doc_text.text, cfg.get('max_chars', 3000)):
        piece = doc_text.text[start:end]
        # PubTator's line format cannot carry newlines/tabs; same-length replacement keeps offsets.
        sanitized = re.sub(r'[\r\n\t]', ' ', piece)
        for bioconcept in cfg.get('bioconcepts', []):
            try:
                raw = client.annotate(sanitized, bioconcept)
            except (HttpError, PubTatorError):
                # Counted and reported; other bioconcepts and documents continue.
                stats[f'freetext_failed_{bioconcept}'] += 1
                continue
            title, found, rows = parse_pubtator(raw)
            if title is not None and title != sanitized:
                stats['freetext_title_mismatch'] += 1
            if rows:
                stats['freetext_relation_rows'] += len(rows)
            for m in found:
                if sanitized[m['start']:m['end']] != m['text']:
                    stats['mention_offset_mismatch'] += 1
                    continue
                key = (start + m['start'], start + m['end'], m['type'])
                if key in seen:
                    continue
                seen.add(key)
                mentions.append(dict(m, start=key[0], end=key[1]))
    return sorted(mentions, key=lambda m: (m['start'], m['end']))


# ---------------------------------------------------------------- evidence

def shortest_cosentence(text, forms_a, forms_b):
    """Shortest sentence containing a surface form of both entities (case-insensitive)."""
    best = None
    a = [f.casefold() for f in forms_a if f]
    b = [f.casefold() for f in forms_b if f]
    for start, end in sentences(text):
        s = text[start:end].casefold()
        if any(f in s for f in a) and any(f in s for f in b):
            if best is None or end - start < best[1] - best[0]:
                best = (start, end)
    return best


def read_id_map(path):
    import csv
    result = {}
    if path and Path(path).is_file():
        with open(path, newline='', encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                result[str(row.get('doc_id', '')).strip()] = dict(pmid=(row.get('pmid') or '').strip() or None,
                                                                   pmcid=(row.get('pmcid') or '').strip() or None)
    return result


# -------------------------------------------------------------------- runs

def _builder(documents, policy, prefix, system):
    rel = load_json(ROOT / 'mappings' / 'pubtator3_relations.json')
    types = load_json(ROOT / 'mappings' / 'pubtator3_entity_types.json')
    type_map = {k: v['type'] for k, v in types['types'].items() if v.get('review') in {'confirmed', 'needs_review'}}
    builder = GraphBuilder(system, prefix, documents, policy, entity_type_map=type_map, relation_map=rel['rules'])
    return builder, digest(rel), digest(types)


def collect_entities(documents, builder, client, cfg):
    stats = Counter()
    per_doc = {}
    for doc in documents:
        dt = build(doc)
        mentions = ner_document(dt, client, cfg, stats)
        for m in mentions:
            node_id, refs = node_id_for(m['type'], m['identifier'], m['text'])
            m['node_id'] = builder.node(node_id, m['text'], m['type'], xrefs=refs)
        per_doc[str(doc['id'])] = (dt, mentions)
    stats['mentions'] = sum(len(m) for _, m in per_doc.values())
    return per_doc, stats


def run_native(documents, policy, cfg, id_map, client):
    builder, rel_hash, type_hash = _builder(documents, policy, 'p3', 'PubTator3')
    per_doc, stats = collect_entities(documents, builder, client, cfg)
    pmids = {doc_id: v['pmid'] for doc_id, v in id_map.items() if v.get('pmid')}
    exports = client.export(sorted(set(pmids.values()))) if pmids else {}
    for doc in documents:
        doc_id = str(doc['id'])
        dt, mentions = per_doc[doc_id]
        forms = defaultdict(set)
        for m in mentions:
            forms[m['node_id']].add(m['text'])
        exported = exports.get(pmids.get(doc_id)) or {}
        for rel in exported.get('relations') or []:
            info = rel.get('infons') or {}
            ends = []
            for role in ('role1', 'role2'):
                r = info.get(role) or {}
                node_id, refs = node_id_for(r.get('type'), r.get('identifier'), r.get('name') or '')
                builder.node(node_id, r.get('name') or node_id, r.get('type'), xrefs=refs)
                forms[node_id].add(r.get('name') or '')
                ends.append(node_id)
            span = shortest_cosentence(dt.text, forms[ends[0]], forms[ends[1]])
            quote, hint = (dt.text[span[0]:span[1]], (dt.locate(*span) or [None])[0]) if span else (None, None)
            builder.claim(ends[0], info.get('type'), ends[1], doc_id, quote=quote, hint_block=hint,
                          evidence_method='pubtator_precomputed_doc_level',
                          extra={'pubtator': {'pmid': pmids.get(doc_id), 'relation_id': rel.get('id'),
                                              'score': info.get('score')}})
            stats['precomputed_relations'] += 1
    stats.update(client.stats)
    return builder, dict(stats=dict(stats), relation_mapping_sha256=rel_hash, type_mapping_sha256=type_hash,
                         documents_with_pmid=len(pmids))


def windows(text, size, overlap):
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(' ', start + size // 2, end)
            end = cut if cut > start else end
        out.append((start, end))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return out


def predicate_catalog(policy):
    predicates, _, _ = ontology(policy)
    return [dict(name=k, definition=v.get('definition'), subject_types=v.get('subject_types'),
                 object_types=v.get('object_types'), required_qualifiers=v.get('required_qualifiers', []),
                 symmetric=v.get('symmetric')) for k, v in predicates.items()]


def run_llm(documents, policy, cfg, client, llm, cache_root, concurrency=2):
    builder, rel_hash, type_hash = _builder(documents, policy, 'p3l', 'PubTator3+LLM')
    per_doc, stats = collect_entities(documents, builder, client, cfg)
    prompt = PROMPT_PATH.read_text(encoding='utf-8')
    prompt_sha = sha256_text(prompt)
    catalog = predicate_catalog(policy)
    predicates, _, parents = ontology(policy)
    cache = Cache(cache_root, 'pubtator3-llm')
    usage = Counter()
    jobs = []
    size, overlap = cfg.get('window_chars', 12000), cfg.get('window_overlap', 800)
    for doc in documents:
        dt, mentions = per_doc[str(doc['id'])]
        for ws, we in windows(dt.text, size, overlap):
            inside = [m for m in mentions if m['start'] >= ws and m['end'] <= we]
            local, names = {}, defaultdict(Counter)
            for m in inside:
                local.setdefault(m['node_id'], f'E{len(local) + 1}')
                names[m['node_id']][m['text']] += 1
            if len(local) < 2:
                stats['windows_skipped_lt2_entities'] += 1
                continue
            entities = [dict(id=lid, name=names[nid].most_common(1)[0][0], type=builder.nodes[nid]['types'][0],
                             pubtator_id=(builder.nodes[nid]['xrefs'] or [None])[0]) for nid, lid in local.items()]
            jobs.append((str(doc['id']), dt, ws, we, local, entities))

    def call(job):
        doc_id, dt, ws, we, local, entities = job
        window = dt.text[ws:we]
        key = sha256_text(f'{prompt_sha}\0{sha256_text(window)}\0{llm.model}')
        cached = cache.get(key)
        if cached is not None:
            return job, cached, True
        user = json.dumps(dict(WINDOW=window, ENTITIES=entities, PREDICATES=catalog), ensure_ascii=False)
        last = None
        for _ in range(2):  # at most one retry per window
            try:
                value, used, finish = llm.ask(prompt, user)
                if isinstance(value, dict) and isinstance(value.get('relations'), list):
                    return job, cache.put(key, dict(value=value, usage=used, finish_reason=finish)), False
                last = 'invalid_structure'
            except LLMError as exc:
                last = str(exc)
        return job, dict(error=last), False

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(call, jobs))
    seen = set()
    for (doc_id, dt, ws, we, local, entities), result, hit in results:
        stats['llm_windows'] += 1
        stats['llm_cache_hits'] += int(hit)
        if 'error' in result:
            stats['llm_window_failures'] += 1
            builder.drops['window_failed'] += 1
            continue
        if not hit:
            stats['llm_calls'] += 1
            for k, v in (result.get('usage') or {}).items():
                if isinstance(v, int):
                    usage[k] += v
        by_local = {lid: nid for nid, lid in local.items()}
        window = dt.text[ws:we]
        for item in result['value']['relations']:
            if not isinstance(item, dict):
                builder.drops['not_object'] += 1
                continue
            s, o, p = by_local.get(item.get('subject')), by_local.get(item.get('object')), item.get('predicate')
            quote = item.get('evidence_quote')
            if not s or not o:
                builder.drops['unknown_entity'] += 1
                continue
            if p not in predicates:
                builder.drops['predicate_not_in_policy'] += 1
                continue
            spec = predicates[p]
            if not any(type_allowed(t, spec.get('subject_types') or ['*'], parents) for t in builder.nodes[s]['types']) or \
                    not any(type_allowed(t, spec.get('object_types') or ['*'], parents) for t in builder.nodes[o]['types']):
                builder.drops['domain_range'] += 1
                continue
            if not isinstance(quote, str) or not quote or quote not in window:
                builder.drops['quote_not_verbatim'] += 1
                continue
            key = (doc_id, s, p, o, item.get('negated'), quote)
            if key in seen:
                builder.drops['duplicate_across_windows'] += 1
                continue
            seen.add(key)
            offset = ws + window.find(quote)
            hint = (dt.locate(offset, offset + len(quote)) or [None])[0]
            qualifiers = item.get('qualifiers') if isinstance(item.get('qualifiers'), dict) else {}
            extra = {k: item[k] for k in ('negated', 'speculative') if isinstance(item.get(k), bool)}
            builder.claim(s, p, o, doc_id, quote=quote, hint_block=hint, qualifiers=qualifiers,
                          evidence_method='llm_on_pubtator_entities', extra=extra)
    stats.update(client.stats)
    return builder, dict(stats=dict(stats, llm_usage=dict(usage), window_chars=size, window_overlap=overlap),
                         prompt_sha256=prompt_sha, relation_mapping_sha256=rel_hash, type_mapping_sha256=type_hash)
