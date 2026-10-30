"""Scoped claim selection and source-preserving bulk deletion."""
from app.catalog import archive_claims, digest
from app.claim_semantics import result_sql
from app.graph_store import lock_graph_projection, prune_orphan_projection
from app.normalization import normalize_entity_type, normalize_predicate


def selection_rows(cur, knowledge_base_id, selection):
    clauses=['m.knowledge_base_id=%s']
    params=[knowledge_base_id]
    if not selection.all_matching:
        clauses.append('c.id=ANY(%s::BIGINT[])');params.append(selection.claim_ids)
    if selection.predicate:
        clauses.append('c.predicate=%s');params.append(normalize_predicate(selection.predicate))
    if selection.entity_type:
        clauses.append('(c.subject_type=%s OR c.object_type=%s)');params.extend([normalize_entity_type(selection.entity_type)]*2)
    if selection.result_status:
        clauses.append(f'({result_sql("c")})=%s');params.append(selection.result_status)
    cur.execute(f"""SELECT c.id,c.document_id,c.claim_fingerprint,d.pipeline_generation
        FROM claims c JOIN documents d ON d.id=c.document_id
        JOIN knowledge_base_documents m ON m.document_id=c.document_id
        WHERE {' AND '.join(clauses)} ORDER BY c.id""",params)
    rows=cur.fetchall()
    if not selection.all_matching and {r['id'] for r in rows} != set(selection.claim_ids):
        raise ValueError('Selected claims are missing, outside this knowledge base, or no longer match the filters')
    return rows


def preview_claim_delete(cur, knowledge_base_id, selection):
    rows=selection_rows(cur,knowledge_base_id,selection)
    ids=sorted({r['document_id'] for r in rows})
    cur.execute('SELECT DISTINCT knowledge_base_id FROM knowledge_base_documents WHERE document_id=ANY(%s::BIGINT[]) AND knowledge_base_id<>%s ORDER BY knowledge_base_id',(ids,knowledge_base_id))
    shared=[r['knowledge_base_id'] for r in cur.fetchall()]
    return {'count':len(rows),'document_ids':ids,'other_affected_knowledge_bases':shared,
            'selection_hash':digest({'claims':[(r['id'],r['document_id'],r['claim_fingerprint'],r['pipeline_generation']) for r in rows],'shared_knowledge_bases':shared}),
            'pdfs_preserved':True,'chunks_preserved':True}


def delete_selected_claims(cur, knowledge_base_id, request):
    preview=preview_claim_delete(cur,knowledge_base_id,request)
    if preview['selection_hash'] != request.selection_hash:
        raise ValueError('Selection changed; refresh the deletion preview')
    if preview['other_affected_knowledge_bases'] and not request.allow_shared:
        raise ValueError('Selected documents are shared; explicitly confirm the listed knowledge bases')
    docs=preview['document_ids']
    if not docs:
        return {**preview,'deleted_count':0,'affected_document_ids':[]}
    cur.execute('SELECT id FROM chunks WHERE document_id=ANY(%s::BIGINT[]) ORDER BY id',(docs,))
    keys=[f'litkg-document:{doc}' for doc in docs]+[f"litkg-chunk:{r['id']}" for r in cur.fetchall()]
    for key in keys:
        cur.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired',(key,))
        if not cur.fetchone()['acquired']:
            raise ValueError('Selected documents are being processed; wait or cancel their tasks first')
    lock_graph_projection(cur,True)
    # Graph-first order matches knowledge-base deletion. Recheck the preview
    # after membership is stable, including any newly shared knowledge base.
    cur.execute('LOCK TABLE knowledge_base_documents IN SHARE MODE')
    cur.execute('SELECT id,status FROM documents WHERE id=ANY(%s::BIGINT[]) ORDER BY id FOR UPDATE',(docs,))
    if any(r['status'] in {'queued','parsing','chunked','cataloging','extracting'} for r in cur.fetchall()):
        raise ValueError('Selected documents have active processing; wait or cancel their tasks first')
    cur.execute("""SELECT EXISTS(SELECT 1 FROM maintenance_jobs j WHERE status IN ('queued','processing') AND
        (j.document_id=ANY(%s::BIGINT[]) OR (j.document_id IS NULL AND EXISTS
        (SELECT 1 FROM knowledge_base_documents m WHERE m.knowledge_base_id=j.knowledge_base_id AND m.document_id=ANY(%s::BIGINT[]))))) AS active""",(docs,docs))
    if cur.fetchone()['active']:
        raise ValueError('A queued or running maintenance task can recreate these claims; cancel it first')
    cur.execute("""SELECT EXISTS(SELECT 1 FROM graph_rebuild_jobs j WHERE status IN ('queued','processing') AND EXISTS
        (SELECT 1 FROM knowledge_base_documents m WHERE m.knowledge_base_id=j.knowledge_base_id AND m.document_id=ANY(%s::BIGINT[]))) AS active""",(docs,))
    if cur.fetchone()['active']:
        raise ValueError('A graph rebuild is pending for these documents; wait or cancel it first')
    current=preview_claim_delete(cur,knowledge_base_id,request)
    if current['selection_hash'] != request.selection_hash:
        raise ValueError('Selection changed; refresh the deletion preview')
    rows=selection_rows(cur,knowledge_base_id,request)
    claim_ids=[r['id'] for r in rows]
    for doc in docs:
        archive_claims(cur,doc)
    cur.execute('DELETE FROM claims WHERE id=ANY(%s::BIGINT[])',(claim_ids,))
    deleted=cur.rowcount
    cur.execute('UPDATE documents SET pipeline_generation=pipeline_generation+1,extraction_epoch=extraction_epoch+1,updated_at=now() WHERE id=ANY(%s::BIGINT[])',(docs,))
    cur.execute("DELETE FROM processing_cache WHERE document_id=ANY(%s::BIGINT[]) AND stage IN ('claims','claims_split')",(docs,))
    cur.execute("""UPDATE review_items SET status='resolved',note='Selected claims deleted by user; see claim_history',updated_at=now()
        WHERE document_id=ANY(%s::BIGINT[]) AND (detail->>'claim_id'=ANY(%s::TEXT[]) OR
        (kind='semantic_conflict' AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(detail->'fact_ids')='array' THEN detail->'fact_ids' ELSE '[]'::jsonb END) AS fid(value)
        WHERE NOT EXISTS(SELECT 1 FROM facts WHERE id::TEXT=fid.value))))""",(docs,[str(cid) for cid in claim_ids]))
    affected=[knowledge_base_id,*preview['other_affected_knowledge_bases']]
    cur.execute('DELETE FROM derived_content WHERE knowledge_base_id=ANY(%s::BIGINT[])',(affected,))
    cur.execute("""INSERT INTO derived_dirty_documents(document_id) SELECT DISTINCT document_id FROM knowledge_base_documents
        WHERE knowledge_base_id=ANY(%s::BIGINT[]) ON CONFLICT(document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1""",(affected,))
    pruned=prune_orphan_projection(cur)
    return {**preview,'deleted_count':deleted,'affected_document_ids':docs,'pruned':pruned}
