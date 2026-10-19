"""Normalize explicit comparison encodings without deleting claims or evidence.

Defaults to a dry run. Apply requires a persistent backup directory. Run with
the application dependencies and PYTHONPATH=<repository>/app.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.catalog import digest
from app.facts import canonical_comparison_qualifiers
from app.graph_store import lock_graph_projection
from app.normalization import claim_fingerprint, entity_identity, qualifiers_fingerprint
from app.schemas import Claim


def fingerprint(row, qualifiers):
    claim = Claim.model_validate(row['claim_json'])
    return digest([claim_fingerprint(
        subject=replace(entity_identity(row['subject_text'], row['subject_type']),
                        canonical_key=f"entity:{row['subject_entity_id']}"),
        predicate=row['predicate'],
        object_=replace(entity_identity(row['object_text'], row['object_type']),
                        canonical_key=f"entity:{row['object_entity_id']}"),
        qualifiers=qualifiers, evidence_text=row['evidence_text'],
        negated=row['negated'], speculative=row['speculative']),
        [e.model_dump() for e in claim.additional_evidence]])


def repair(conn, batch_id, backup_dir=None, apply=False):
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout='5s'")
    cur.execute("SET LOCAL statement_timeout='60s'")
    lock_graph_projection(cur, reconcile_existing=True)
    cur.execute('''SELECT d.id,d.status FROM documents d WHERE d.id IN
        (SELECT document_id FROM ingestion_batch_items WHERE batch_id=%s) ORDER BY d.id FOR UPDATE''', (batch_id,))
    documents = cur.fetchall()
    if not documents or any(d['status'] not in {'done', 'partial', 'failed'} for d in documents):
        raise ValueError('Batch must exist and all source documents must be idle')
    doc_ids = [d['id'] for d in documents]
    cur.execute('''SELECT r.* FROM relations r WHERE r.predicate='DIFFERS_BETWEEN'
        AND EXISTS (SELECT 1 FROM relation_evidence re JOIN claims c ON c.id=re.claim_id
                    WHERE re.relation_id=r.id AND c.document_id=ANY(%s)) ORDER BY r.id FOR UPDATE''', (doc_ids,))
    relations = cur.fetchall()
    changes = []
    for relation in relations:
        normalized = canonical_comparison_qualifiers(relation['predicate'], relation['qualifiers'])
        if normalized == relation['qualifiers']:
            continue
        new_hash = qualifiers_fingerprint(normalized)
        cur.execute('''SELECT * FROM relations WHERE subject_entity_id=%s AND predicate=%s AND object_entity_id=%s
            AND qualifiers_hash=%s AND negated=%s AND speculative=%s FOR UPDATE''',
            (relation['subject_entity_id'], relation['predicate'], relation['object_entity_id'],
             new_hash, relation['negated'], relation['speculative']))
        target = cur.fetchone()
        if target and target['qualifiers'] != normalized:
            raise ValueError('Hash-equivalent qualifiers differ in representation; inspect manually')
        changes.append({'source': relation, 'target': target, 'qualifiers': normalized, 'hash': new_hash})
    result = {'batch_id': batch_id, 'normalized_relations': len(changes),
              'merged_relations': sum(c['target'] is not None for c in changes),
              'relation_map': {c['source']['id']: c['target']['id'] if c['target'] else c['source']['id'] for c in changes}}
    if not changes:
        return {**result, 'applied': False}
    source_ids = [c['source']['id'] for c in changes]
    all_ids = sorted(set(source_ids + [c['target']['id'] for c in changes if c['target']]))
    changes_by_id = {c['source']['id']: c for c in changes}
    cur.execute('''SELECT c.*, re.relation_id FROM claims c JOIN relation_evidence re ON re.claim_id=c.id
        WHERE re.relation_id=ANY(%s) ORDER BY c.id FOR UPDATE OF c''', (source_ids,))
    claims = cur.fetchall()
    # A selected relation can be shared by other documents; do not change active sources.
    affected_doc_ids = sorted({c['document_id'] for c in claims})
    cur.execute('SELECT id,status FROM documents WHERE id=ANY(%s) ORDER BY id FOR UPDATE', (affected_doc_ids,))
    if any(d['status'] not in {'done', 'partial', 'failed'} for d in cur.fetchall()):
        raise ValueError('A shared source document is processing; retry after it finishes')
    claim_ids = [c['id'] for c in claims]
    cur.execute('SELECT * FROM facts WHERE claim_id=ANY(%s) ORDER BY id FOR UPDATE', (claim_ids,))
    facts = cur.fetchall()
    if len(facts) != len(claims):
        raise ValueError('Missing complete facts; inspect before repair')
    retained_fingerprints = []
    for claim in claims:
        change = changes_by_id[claim['relation_id']]
        if fingerprint(claim, change['source']['qualifiers']) != claim['claim_fingerprint']:
            raise ValueError(f"Claim {claim['id']} has an unexpected projection fingerprint")
        claim['new_fingerprint'] = fingerprint(claim, change['qualifiers'])
        cur.execute('SELECT id FROM claims WHERE chunk_id=%s AND claim_fingerprint=%s AND id<>%s',
                    (claim['chunk_id'], claim['new_fingerprint'], claim['id']))
        collision = cur.fetchone()
        if collision:
            # Two legacy encodings can share both chunk and evidence. Keep both
            # source records; future retries select the existing canonical row.
            retained_fingerprints.append({'claim_id': claim['id'], 'canonical_claim_id': collision['id']})
            claim['new_fingerprint'] = claim['claim_fingerprint']
    result['retained_legacy_fingerprints'] = retained_fingerprints
    if not apply:
        return {**result, 'affected_claims': len(claims), 'applied': False}
    if backup_dir is None:
        raise ValueError('--backup-dir is required with --apply')
    snapshot = {'batch_id': batch_id, 'changes': changes, 'claims': claims, 'facts': facts}
    for table, clause, values in [
        ('relation_evidence', 'relation_id=ANY(%s)', all_ids),
        ('fact_participants', 'fact_id=ANY(%s)', [f['id'] for f in facts]),
        ('fact_evidence', 'fact_id=ANY(%s)', [f['id'] for f in facts]),
        ('derived_dirty_documents', 'document_id=ANY(%s)', affected_doc_ids),
    ]:
        cur.execute(f'SELECT * FROM {table} WHERE {clause}', (values,))
        snapshot[table] = cur.fetchall()
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / f"batch{batch_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.json"
    with backup.open('x', encoding='utf-8') as stream:
        os.chmod(backup, 0o600)
        json.dump(snapshot, stream, ensure_ascii=False, default=str, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for change in changes:
        source_id = change['source']['id']
        target_id = change['target']['id'] if change['target'] else source_id
        if not change['target']:
            cur.execute('UPDATE relations SET qualifiers=%s,qualifiers_hash=%s,updated_at=now() WHERE id=%s',
                        (Jsonb(change['qualifiers']), change['hash'], source_id))
        for fact in (f for f in facts if f['relation_id'] == source_id):
            cur.execute('SELECT role,entity_id FROM fact_participants WHERE fact_id=%s ORDER BY role,entity_id', (fact['id'],))
            key = digest({'participants': [(p['role'], p['entity_id']) for p in cur.fetchall()],
                'predicate': fact['predicate'], 'qualifiers': change['qualifiers'],
                'negated': fact['negated'], 'speculative': fact['speculative']})
            cur.execute('UPDATE facts SET relation_id=%s,qualifiers=%s,fact_key=%s,updated_at=now() WHERE id=%s',
                        (target_id, Jsonb(change['qualifiers']), key, fact['id']))
        cur.execute('UPDATE relation_evidence SET relation_id=%s WHERE relation_id=%s', (target_id, source_id))
        if change['target']:
            cur.execute('DELETE FROM relations WHERE id=%s', (source_id,))
    for claim in claims:
        cur.execute('UPDATE claims SET claim_fingerprint=%s WHERE id=%s', (claim['new_fingerprint'], claim['id']))
    cur.execute('SELECT id,claim_json FROM claims WHERE id=ANY(%s) ORDER BY id', (claim_ids,))
    assert cur.fetchall() == [{'id': c['id'], 'claim_json': c['claim_json']} for c in claims], 'Raw claims changed'
    cur.execute('''SELECT count(*) AS count FROM relation_evidence re JOIN facts f ON f.claim_id=re.claim_id
        WHERE re.claim_id=ANY(%s) AND re.relation_id=f.relation_id''', (claim_ids,))
    assert cur.fetchone()['count'] == len(claims), 'Evidence/fact linkage changed'
    cur.execute('''INSERT INTO derived_dirty_documents(document_id) SELECT unnest(%s::BIGINT[])
        ON CONFLICT(document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1''', (affected_doc_ids,))
    return {**result, 'affected_claims': len(claims), 'affected_documents': affected_doc_ids,
            'backup': str(backup), 'applied': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-id', type=int, required=True)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).resolve().parents[1] / '.env')
    parser.add_argument('--host', help='Override Docker hostname when running on the host')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    env = dict(line.split('=', 1) for line in args.env_file.read_text().splitlines()
               if line.strip() and not line.lstrip().startswith('#') and '=' in line)
    params = conninfo_to_dict(os.environ.get('DATABASE_URL') or env['DATABASE_URL'].strip('\"\''))
    if args.host:
        params['host'] = args.host
    with psycopg.connect(**params, row_factory=dict_row) as conn:
        result = repair(conn, args.batch_id, args.backup_dir, args.apply)
        if not args.apply:
            conn.rollback()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
