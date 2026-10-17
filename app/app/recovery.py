"""Conservative orphan detection: age alone never proves a task was lost."""
from contextlib import contextmanager
import logging
import os
import threading
import uuid

import redis

log = logging.getLogger(__name__)
ACTIVE = ['uploaded','queued','parsing','chunked','cataloging','extracting']


@contextmanager
def heartbeat_lease(conn, connection_factory, resource):
    owner = str(uuid.uuid4())
    conn.execute("""INSERT INTO processing_leases (resource,owner,heartbeat_at)
        VALUES (%s,%s::UUID,now()) ON CONFLICT (resource) DO UPDATE
        SET owner=EXCLUDED.owner,heartbeat_at=now()""", (resource,owner))
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(30):
            try:
                with connection_factory() as heartbeat_conn:
                    heartbeat_conn.execute('UPDATE processing_leases SET heartbeat_at=now() WHERE resource=%s AND owner=%s::UUID',
                                           (resource,owner))
            except Exception:
                log.warning('Processing lease heartbeat failed for %s', resource, exc_info=True)
    thread = threading.Thread(target=heartbeat, name='litkg-lease', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        conn.execute('DELETE FROM processing_leases WHERE resource=%s AND owner=%s::UUID', (resource,owner))


def broker_is_idle():
    """Redis pending AND unacknowledged messages must be absent. Fail closed."""
    try:
        client = redis.Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=1, socket_timeout=1)
        try:
            with client.pipeline(transaction=True) as pipe:
                pipe.hlen('unacked')
                pipe.zcard('unacked_index')
                for queue in ('parse','extract','celery'):
                    for priority in (None,3,6,9):
                        pipe.llen(queue if priority is None else f'{queue}\x06\x16{priority}')
                return not any(pipe.execute())
        finally:
            client.close()
    except Exception:
        return False


def reconcile_stalled(cur, *, batch_id=None, document_id=None):
    timeout = max(300, int(os.environ.get('PIPELINE_STALE_SECONDS','1800')))
    cur.execute("""SELECT d.id FROM documents d WHERE d.status=ANY(%s::TEXT[])
        AND (%s::BIGINT IS NULL OR d.id=%s)
        AND (%s::BIGINT IS NULL OR EXISTS (SELECT 1 FROM ingestion_batch_items i WHERE i.batch_id=%s AND i.document_id=d.id))
        AND GREATEST(d.created_at,
            CASE WHEN d.status IN ('uploaded','queued') THEN d.updated_at ELSE d.created_at END,
            COALESCE((SELECT max(c.updated_at) FROM chunks c WHERE c.document_id=d.id),d.created_at),
            COALESCE((SELECT max(a.created_at) FROM processing_attempts a WHERE a.document_id=d.id),d.created_at))
            < now() - (%s * interval '1 second') ORDER BY d.id""",
        (ACTIVE,document_id,document_id,batch_id,batch_id,timeout))
    candidates = cur.fetchall()
    if not candidates or not broker_is_idle():
        return []
    recovered = []
    for candidate in candidates:
        doc_id = candidate['id']
        # Live leases conservatively protect all work, including maintenance
        # between chunks. Legacy workers are protected by their advisory locks.
        cur.execute("SELECT EXISTS (SELECT 1 FROM processing_leases WHERE heartbeat_at > now()-interval '120 seconds') AS active")
        if cur.fetchone()['active']:
            break
        cur.execute('SELECT id FROM chunks WHERE document_id=%s ORDER BY id', (doc_id,))
        keys = [f'litkg-document:{doc_id}', *[f"litkg-chunk:{row['id']}" for row in cur.fetchall()]]
        cur.execute("""SELECT j.id FROM maintenance_jobs j WHERE j.status IN ('queued','processing')
            AND (j.document_id=%s OR (j.document_id IS NULL AND EXISTS
                (SELECT 1 FROM knowledge_base_documents m WHERE m.knowledge_base_id=j.knowledge_base_id AND m.document_id=%s)))""", (doc_id,doc_id))
        jobs = cur.fetchall()
        keys.extend(f"litkg-maintenance:{job['id']}" for job in jobs)
        locked = True
        for key in keys:
            cur.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired', (key,))
            if not cur.fetchone()['acquired']:
                locked = False
                break
        if not locked or not broker_is_idle():
            continue
        message = 'Pipeline stalled: no queued/unacknowledged broker work, live lease or processing lock; retry resumes saved stages'
        cur.execute("""UPDATE chunks SET status='failed',error=%s,updated_at=now()
            WHERE document_id=%s AND status IN ('queued','extracting')""", (message,doc_id))
        cur.execute("""UPDATE documents d SET status=CASE WHEN EXISTS
            (SELECT 1 FROM chunks c WHERE c.document_id=d.id AND c.status IN ('done','partial')) THEN 'partial' ELSE 'failed' END,
            error=%s, catalog_status=CASE WHEN catalog_status='processing' THEN 'failed' ELSE catalog_status END,
            catalog_error=CASE WHEN catalog_status='processing' THEN %s ELSE catalog_error END,updated_at=now()
            WHERE id=%s AND status=ANY(%s::TEXT[])""", (message,message,doc_id,ACTIVE))
        for job in jobs:
            cur.execute("""UPDATE maintenance_jobs SET status='failed',error=%s,updated_at=now()
                WHERE id=%s AND status IN ('queued','processing')""", (message,job['id']))
        recovered.append(doc_id)
    return recovered
