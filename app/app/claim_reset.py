"""Preview-first removal of a KB's active claims; never removes PDFs or chunks.

Run with stopped parse/extract workers and no new uploads/maintenance requests.
An explicit --include-shared is required if documents belong to other KBs.
"""
import argparse
import json

from app.catalog import archive_claims
from app.graph_store import lock_graph_projection, prune_orphan_projection
from app.recovery import broker_is_idle


def reset_claims(cur, knowledge_base_id, *, apply=False, include_shared=False, idle_check=broker_is_idle):
    if apply:
        cur.execute('LOCK TABLE knowledge_base_documents IN SHARE MODE')
    cur.execute('SELECT id FROM knowledge_bases WHERE id=%s', (knowledge_base_id,))
    if not cur.fetchone():
        raise ValueError('Knowledge base does not exist')
    cur.execute('SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id=%s ORDER BY document_id', (knowledge_base_id,))
    ids = [r['document_id'] for r in cur.fetchall()]
    cur.execute('SELECT DISTINCT knowledge_base_id FROM knowledge_base_documents WHERE document_id=ANY(%s::BIGINT[]) ORDER BY knowledge_base_id', (ids,))
    affected = [r['knowledge_base_id'] for r in cur.fetchall()]
    shared = [kb for kb in affected if kb != knowledge_base_id]
    cur.execute('SELECT count(*) AS n FROM claims WHERE document_id=ANY(%s::BIGINT[])', (ids,))
    count = cur.fetchone()['n']
    preview = {'knowledge_base_id':knowledge_base_id, 'document_ids':ids, 'claim_count':count,
               'other_affected_knowledge_bases':shared, 'pdfs_preserved':True, 'chunks_preserved':True,
               'history_preserved':True, 'applied':False}
    if not apply:
        return preview
    if shared and not include_shared:
        raise ValueError('Documents are shared with other knowledge bases; inspect preview and use --include-shared explicitly')
    if not idle_check():
        raise ValueError('Broker is not idle or cannot be checked; wait for work to finish and stop workers')
    cur.execute("SELECT EXISTS (SELECT 1 FROM processing_leases WHERE heartbeat_at>now()-interval '120 seconds') AS active")
    if cur.fetchone()['active']:
        raise ValueError('A processing lease is active; wait for work to finish')
    cur.execute('SELECT id FROM chunks WHERE document_id=ANY(%s::BIGINT[]) ORDER BY id', (ids,))
    keys = [f'litkg-document:{doc}' for doc in ids] + [f"litkg-chunk:{r['id']}" for r in cur.fetchall()]
    for key in keys:
        cur.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired', (key,))
        if not cur.fetchone()['acquired']:
            raise ValueError('Selected document/chunk is busy; no changes applied')
    lock_graph_projection(cur, True)
    cur.execute('SELECT id,status FROM documents WHERE id=ANY(%s::BIGINT[]) ORDER BY id FOR UPDATE', (ids,))
    if any(r['status'] in {'queued','parsing','chunked','cataloging','extracting'} for r in cur.fetchall()):
        raise ValueError('Selected documents have unfinished processing; finish/recover it before reset')
    cur.execute("""SELECT EXISTS (SELECT 1 FROM maintenance_jobs j WHERE j.status IN ('queued','processing')
        AND (j.document_id=ANY(%s::BIGINT[]) OR j.knowledge_base_id=ANY(%s::BIGINT[]))
        UNION ALL SELECT 1 FROM graph_rebuild_jobs WHERE status IN ('queued','processing')
        AND knowledge_base_id=ANY(%s::BIGINT[])) AS active""", (ids,affected,affected))
    if cur.fetchone()['active'] or not idle_check():
        raise ValueError('Maintenance/rebuild/broker work is pending; no changes applied')
    for document_id in ids:
        archive_claims(cur, document_id)
    cur.execute('DELETE FROM claims WHERE document_id=ANY(%s::BIGINT[])', (ids,))
    deleted = cur.rowcount
    # Generations fence already-dispatched extraction messages. A fresh catalog
    # epoch and cache removal prevent retrying the same stale model response.
    cur.execute("""UPDATE documents SET pipeline_generation=pipeline_generation+1,
        extraction_epoch=extraction_epoch+1, updated_at=now()
        WHERE id=ANY(%s::BIGINT[])""", (ids,))
    cur.execute("UPDATE chunks SET extraction_version=NULL,updated_at=now() WHERE document_id=ANY(%s::BIGINT[])", (ids,))
    cur.execute("DELETE FROM processing_cache WHERE document_id=ANY(%s::BIGINT[]) AND stage='claims'", (ids,))
    cur.execute("""UPDATE review_items SET status='resolved',note='Active claims explicitly cleared; see claim_history',updated_at=now()
        WHERE document_id=ANY(%s::BIGINT[]) AND (kind IN ('claim_rejection','claim_repair','processing_gap','semantic_split','semantic_conflict')
        OR detail ? 'claim_id')""", (ids,))
    cur.execute('DELETE FROM derived_content WHERE knowledge_base_id=ANY(%s::BIGINT[])', (affected,))
    cur.execute("""INSERT INTO derived_dirty_documents(document_id)
        SELECT DISTINCT document_id FROM knowledge_base_documents WHERE knowledge_base_id=ANY(%s::BIGINT[])
        ON CONFLICT(document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1""", (affected,))
    pruned = prune_orphan_projection(cur)
    cur.execute('SELECT count(*) AS n FROM claims WHERE document_id=ANY(%s::BIGINT[])', (ids,))
    if cur.fetchone()['n']:
        raise ValueError('Reset postcondition failed; transaction must roll back')
    return {**preview, 'applied':True, 'deleted_claims':deleted, 'pruned':pruned}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge-base-id', required=True, type=int)
    parser.add_argument('--apply', action='store_true', help='Commit deletion; otherwise only preview counts and shared scope')
    parser.add_argument('--include-shared', action='store_true')
    args = parser.parse_args()
    from app.db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                result = reset_claims(cur, args.knowledge_base_id, apply=args.apply, include_shared=args.include_shared)
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
