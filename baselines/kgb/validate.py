"""Output-contract checks; optional read-only QC normalize() dry run."""
import re
import sys
from pathlib import Path

from .common import digest, load_json

SECRET_PATTERNS = [re.compile(p) for p in (r'\bsk-[A-Za-z0-9_-]{16,}', r'Bearer\s+[A-Za-z0-9._-]{12,}',
                                            r'(?i)"(api[_-]?key|password|token|cookie)"\s*:\s*"[^"]+"')]
PREFIXES = {'p3': ('p3:', 'p3-'), 'p3l': ('p3:', 'p3l-'), 'nb': ('nb:', 'nb-')}


def validate(path, documents, policy, secrets=(), qc_path=None):
    errors, notes = [], []
    raw = Path(path).read_text(encoding='utf-8')
    for pattern in SECRET_PATTERNS:
        if pattern.search(raw):
            errors.append(f'possible credential pattern {pattern.pattern!r} in output')
    for value in secrets:
        if value and value in raw:
            errors.append('a configured credential value appears in the output')
    graph = load_json(path)
    for key in ('name', 'nodes', 'claims', 'documents', 'policy', 'metadata'):
        if key not in graph:
            errors.append(f'missing top-level key {key}')
    if errors:
        return errors, notes
    node_ids = [n.get('id') for n in graph['nodes']]
    claim_ids = [c.get('id') for c in graph['claims']]
    if len(set(node_ids)) != len(node_ids):
        errors.append('duplicate node ids')
    if len(set(claim_ids)) != len(claim_ids):
        errors.append('duplicate claim ids')
    known = set(node_ids)
    blocks = {(str(d['id']), str(b['id'])): b['text'] for d in graph['documents'] for b in d.get('blocks', [])}
    predicates = set(((graph['policy'].get('ontology') or {}).get('predicates') or {}))
    unanchored = bad_refs = unmapped_without_original = 0
    for c in graph['claims']:
        for role in ('subject', 'object'):
            if (c.get(role) or {}).get('id') not in known:
                errors.append(f'claim {c.get("id")}: {role} not in nodes')
        if not c.get('source_refs'):
            unanchored += 1
        for ref in c.get('source_refs') or []:
            text = blocks.get((str(ref.get('doc_id')), str(ref.get('block_id'))))
            if text is None or not isinstance(ref.get('start'), int) or text[ref['start']:ref['end']] != ref.get('quote'):
                bad_refs += 1
            elif text.count(ref['quote']) != 1:
                bad_refs += 1
        if c.get('predicate') not in predicates and 'original_predicate' not in (c.get('qualifiers') or {}):
            unmapped_without_original += 1
    if bad_refs:
        errors.append(f'{bad_refs} source_refs violate text[start:end] == quote or are not unique in their block')
    if unmapped_without_original:
        errors.append(f'{unmapped_without_original} claims with non-policy predicate lack qualifiers.original_predicate')
    if graph['documents'] != documents:
        errors.append('documents differ from corpus/documents.json')
    if graph['policy'] != policy:
        errors.append('policy differs from corpus/policy.json')
    if graph['metadata'].get('corpus_sha256') != digest(graph['documents']):
        errors.append('metadata.corpus_sha256 does not match digest(documents)')
    system = (claim_ids[0] or '').split('-')[0] if claim_ids else None
    if system in PREFIXES:
        node_prefix, claim_prefix = PREFIXES[system]
        if any(not str(i).startswith(node_prefix) for i in node_ids) or any(not str(i).startswith(claim_prefix) for i in claim_ids):
            errors.append('ids are not system-prefixed')
    notes.append(f'{len(node_ids)} nodes, {len(claim_ids)} claims, {unanchored} unanchored claims (counted, allowed)')
    if qc_path:
        sys.path.insert(0, str(Path(qc_path).resolve()))
        from quality_control.models import digest as qc_digest, normalize  # read-only use
        normalized = normalize(graph)
        notes.append(f'QC normalize OK: snapshot_hash={normalized["snapshot_hash"]} '
                     f'digest(documents)={qc_digest(normalized["documents"])}')
    return errors, notes
