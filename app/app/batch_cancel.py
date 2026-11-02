"""Durable batch cancellation; workers cooperate through document generation fences."""

ACTIVE_DOCUMENT_STATUSES = ('uploaded', 'queued', 'parsing', 'chunked', 'cataloging', 'extracting')
CANCELLABLE_DOCUMENT_STATUSES = (*ACTIVE_DOCUMENT_STATUSES, 'failed', 'partial')
CANCEL_MESSAGE = 'Cancelled by ingestion batch request'


class BatchNotFound(ValueError):
    pass


class BatchAlreadyFinished(ValueError):
    pass


def cancel_batch(cur, batch_id):
    # Two overlapping batches cancelled concurrently must not both decide that
    # the other still needs their shared document. This short transaction never
    # waits on a model call or on the workers' session advisory locks.
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('litkg-batch-cancel', 0))")
    cur.execute('SELECT id,status FROM ingestion_batches WHERE id=%s FOR NO KEY UPDATE', (batch_id,))
    batch = cur.fetchone()
    if not batch:
        raise BatchNotFound('Ingestion batch not found')
    if batch['status'] == 'cancelled':
        return {'batch_id':batch_id, 'status':'cancelled', 'already_cancelled':True,
                'stopped_document_ids':[], 'continued_document_ids':[]}
    if batch['status'] not in {'queued', 'processing'}:
        raise BatchAlreadyFinished('Batch has already finished')

    cur.execute("""UPDATE ingestion_batches SET status='cancelled',error=%s,updated_at=now(),
        accepted_count=(SELECT count(document_id) FROM ingestion_batch_items WHERE batch_id=%s) WHERE id=%s""",
                (CANCEL_MESSAGE, batch_id, batch_id))
    cur.execute("""SELECT d.id,d.status FROM documents d WHERE d.id IN
        (SELECT document_id FROM ingestion_batch_items WHERE batch_id=%s)
        ORDER BY d.id FOR UPDATE""", (batch_id,))
    documents = cur.fetchall()
    stopped, continued = [], []
    for document in documents:
        if document['status'] not in CANCELLABLE_DOCUMENT_STATUSES:
            continue
        doc_id = document['id']
        cur.execute("""SELECT EXISTS (
            SELECT 1 FROM ingestion_batch_items i JOIN ingestion_batches b ON b.id=i.batch_id
            WHERE i.document_id=%s AND b.id<>%s AND b.status<>'cancelled'
              AND i.status=ANY(%s::TEXT[])
            UNION ALL
            SELECT 1 FROM maintenance_jobs j JOIN knowledge_base_documents m
              ON m.knowledge_base_id=j.knowledge_base_id
            WHERE m.document_id=%s AND j.status IN ('queued','processing')
              AND j.mode IN ('retry','reextract','reparse')
              AND (j.document_id IS NULL OR j.document_id=%s)
        ) AS needed""", (doc_id,batch_id,list(ACTIVE_DOCUMENT_STATUSES),doc_id,doc_id))
        if cur.fetchone()['needed']:
            continued.append(doc_id)
            continue
        cur.execute("""UPDATE documents SET status='cancelled',error=%s,
            pipeline_generation=pipeline_generation+1,
            catalog_status=CASE WHEN catalog_status IN ('pending','processing') THEN 'cancelled' ELSE catalog_status END,
            updated_at=now() WHERE id=%s""", (CANCEL_MESSAGE,doc_id))
        cur.execute("""UPDATE chunks SET status='cancelled',error=%s,updated_at=now()
            WHERE document_id=%s AND status IN ('queued','extracting')""", (CANCEL_MESSAGE,doc_id))
        cur.execute("""UPDATE document_catalogs SET status='cancelled',error=%s
            WHERE document_id=%s AND status='processing'""", (CANCEL_MESSAGE,doc_id))
        stopped.append(doc_id)
    # Freeze this batch's view even when a shared document keeps processing.
    cur.execute("""UPDATE ingestion_batch_items SET status='cancelled',error=%s,updated_at=now()
        WHERE batch_id=%s AND status=ANY(%s::TEXT[])""",
        (CANCEL_MESSAGE,batch_id,list(ACTIVE_DOCUMENT_STATUSES)))
    return {'batch_id':batch_id, 'status':'cancelled', 'already_cancelled':False,
            'stopped_document_ids':stopped, 'continued_document_ids':continued}
