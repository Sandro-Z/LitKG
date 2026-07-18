import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)

from app.batch_status import sync_batch_status
from app.db import get_conn
from app.graph_query import fetch_graph
from app.ingestion import (
    UploadRejected,
    register_document,
    safe_filename,
    store_pdf,
)
from app.normalization import normalize_entity_type, normalize_predicate
from app.schemas import KnowledgeBaseCreate
from app.tasks import (
    parse_document,
    rebuild_knowledge_graph,
    update_document_status,
)


UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
MAX_UPLOAD_BYTES = int(
    os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
MAX_BATCH_FILES = int(os.environ.get("MAX_BATCH_FILES", "200"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="LitKG API",
    version="2.0.0",
    description=(
        "Batch literature ingestion with a unified, provenance-preserving "
        "knowledge graph"
    ),
)


def _get_knowledge_base(cur, knowledge_base_id: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT id, slug, name, description, created_at, updated_at
        FROM knowledge_bases
        WHERE id = %s
        """,
        (knowledge_base_id,),
    )
    knowledge_base = cur.fetchone()
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return knowledge_base


def _get_default_knowledge_base_id() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM knowledge_bases WHERE slug = 'default'"
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=503,
            detail="Default knowledge base is missing; apply db/init.sql",
        )
    return row["id"]


def _batch_payload(
    batch_id: int,
    item_metadata: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            sync_batch_status(cur, batch_id)
            cur.execute(
                """
                SELECT
                    batch.id,
                    batch.knowledge_base_id,
                    kb.name AS knowledge_base_name,
                    batch.name,
                    batch.status,
                    batch.submitted_count,
                    batch.accepted_count,
                    batch.error,
                    batch.created_at,
                    batch.updated_at
                FROM ingestion_batches batch
                JOIN knowledge_bases kb ON kb.id = batch.knowledge_base_id
                WHERE batch.id = %s
                """,
                (batch_id,),
            )
            batch = cur.fetchone()
            if not batch:
                raise HTTPException(
                    status_code=404,
                    detail="Ingestion batch not found",
                )

            cur.execute(
                """
                SELECT
                    item.id,
                    item.position,
                    item.filename,
                    item.document_id,
                    item.sha256,
                    item.status,
                    item.error,
                    document.title,
                    document.created_at AS document_created_at,
                    document.updated_at AS document_updated_at
                FROM ingestion_batch_items item
                LEFT JOIN documents document ON document.id = item.document_id
                WHERE item.batch_id = %s
                ORDER BY item.position
                """,
                (batch_id,),
            )
            items = cur.fetchall()

    if item_metadata:
        for item in items:
            item.update(item_metadata.get(item["position"], {}))
    batch["items"] = items
    return batch


def _ingest_files(
    *,
    knowledge_base_id: int,
    files: list[UploadFile],
    batch_name: str | None,
    force_reprocess: bool,
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="At least one PDF is required")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"A batch may contain at most {MAX_BATCH_FILES} files",
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                """
                INSERT INTO ingestion_batches (
                    knowledge_base_id,
                    name,
                    status,
                    submitted_count
                )
                VALUES (%s, %s, 'queued', %s)
                RETURNING id
                """,
                (
                    knowledge_base_id,
                    (batch_name or "").strip()[:200] or None,
                    len(files),
                ),
            )
            batch_id = cur.fetchone()["id"]

    document_ids_to_queue = set()
    item_metadata: dict[int, dict[str, Any]] = {}

    for position, upload in enumerate(files):
        filename = safe_filename(upload.filename)
        try:
            stored = store_pdf(
                upload.file,
                filename,
                UPLOAD_DIR,
                MAX_UPLOAD_BYTES,
            )
            with get_conn() as conn:
                with conn.cursor() as cur:
                    registered = register_document(
                        cur,
                        stored=stored,
                        force_reprocess=force_reprocess,
                    )
                    cur.execute(
                        """
                        INSERT INTO knowledge_base_documents (
                            knowledge_base_id,
                            document_id,
                            added_via_batch_id
                        )
                        VALUES (%s, %s, %s)
                        ON CONFLICT (knowledge_base_id, document_id)
                        DO NOTHING
                        """,
                        (
                            knowledge_base_id,
                            registered.document_id,
                            batch_id,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO ingestion_batch_items (
                            batch_id,
                            position,
                            filename,
                            document_id,
                            sha256,
                            status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            batch_id,
                            position,
                            stored.filename,
                            registered.document_id,
                            stored.sha256,
                            registered.status,
                        ),
                    )

            if registered.queued:
                document_ids_to_queue.add(registered.document_id)
            item_metadata[position] = {
                "size_bytes": stored.size_bytes,
                "was_new_document": registered.inserted,
                "queued_for_processing": registered.queued,
            }
        except UploadRejected as exc:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingestion_batch_items (
                            batch_id,
                            position,
                            filename,
                            status,
                            error
                        )
                        VALUES (%s, %s, %s, 'rejected', %s)
                        """,
                        (batch_id, position, filename, str(exc)),
                    )
            item_metadata[position] = {
                "was_new_document": False,
                "queued_for_processing": False,
            }
        except Exception as exc:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingestion_batch_items (
                            batch_id,
                            position,
                            filename,
                            status,
                            error
                        )
                        VALUES (%s, %s, %s, 'failed', %s)
                        """,
                        (batch_id, position, filename, str(exc)),
                    )
            item_metadata[position] = {
                "was_new_document": False,
                "queued_for_processing": False,
            }

    for document_id in document_ids_to_queue:
        try:
            parse_document.apply_async(args=[document_id], queue="parse")
        except Exception as exc:
            update_document_status(
                document_id,
                "failed",
                f"Could not enqueue document: {exc}",
            )

    return _batch_payload(batch_id, item_metadata=item_metadata)


@app.get("/health")
def health():
    return {"status": "ok", "api_version": "2.0.0"}


@app.post("/knowledge-bases", status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreate):
    slug = f"kb-{uuid.uuid4().hex[:12]}"
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail="Knowledge base name cannot be blank",
        )
    description = (
        payload.description.strip()
        if payload.description and payload.description.strip()
        else None
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge_bases (slug, name, description)
                VALUES (%s, %s, %s)
                RETURNING
                    id, slug, name, description, created_at, updated_at
                """,
                (slug, name, description),
            )
            return cur.fetchone()


@app.get("/knowledge-bases")
def list_knowledge_bases(limit: int = Query(100, ge=1, le=500)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    kb.id,
                    kb.slug,
                    kb.name,
                    kb.description,
                    kb.created_at,
                    kb.updated_at,
                    (
                        SELECT COUNT(*)
                        FROM knowledge_base_documents member
                        WHERE member.knowledge_base_id = kb.id
                    )::INT AS document_count,
                    (
                        SELECT COUNT(*)
                        FROM claims claim
                        JOIN knowledge_base_documents member
                          ON member.document_id = claim.document_id
                        WHERE member.knowledge_base_id = kb.id
                    )::INT AS claim_count,
                    (
                        SELECT COUNT(DISTINCT evidence.relation_id)
                        FROM relation_evidence evidence
                        JOIN claims claim ON claim.id = evidence.claim_id
                        JOIN knowledge_base_documents member
                          ON member.document_id = claim.document_id
                        WHERE member.knowledge_base_id = kb.id
                    )::INT AS relation_count
                FROM knowledge_bases kb
                ORDER BY kb.created_at
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return {"knowledge_bases": rows}


@app.get("/knowledge-bases/{knowledge_base_id}")
def get_knowledge_base(knowledge_base_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            knowledge_base = _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                """
                SELECT
                    COUNT(DISTINCT member.document_id)::INT AS document_count,
                    COUNT(DISTINCT claim.id)::INT AS claim_count,
                    COUNT(DISTINCT evidence.relation_id)::INT AS relation_count
                FROM knowledge_base_documents member
                LEFT JOIN claims claim
                  ON claim.document_id = member.document_id
                LEFT JOIN relation_evidence evidence
                  ON evidence.claim_id = claim.id
                WHERE member.knowledge_base_id = %s
                """,
                (knowledge_base_id,),
            )
            knowledge_base["stats"] = cur.fetchone()
    return {"knowledge_base": knowledge_base}


@app.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    status_code=202,
)
def upload_document_batch(
    knowledge_base_id: int,
    files: list[UploadFile] = File(...),
    batch_name: str | None = Form(None),
    force_reprocess: bool = Form(False),
):
    return _ingest_files(
        knowledge_base_id=knowledge_base_id,
        files=files,
        batch_name=batch_name,
        force_reprocess=force_reprocess,
    )


@app.get("/knowledge-bases/{knowledge_base_id}/documents")
def list_knowledge_base_documents(
    knowledge_base_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                """
                SELECT
                    document.id,
                    document.sha256,
                    document.filename,
                    document.title,
                    document.status,
                    document.error,
                    document.created_at,
                    document.updated_at,
                    COUNT(DISTINCT chunk.id)::INT AS chunk_count,
                    COUNT(DISTINCT chunk.id) FILTER (
                        WHERE chunk.status = 'done'
                    )::INT AS chunk_done_count,
                    COUNT(DISTINCT chunk.id) FILTER (
                        WHERE chunk.status = 'failed'
                    )::INT AS chunk_failed_count,
                    COUNT(DISTINCT claim.id)::INT AS claim_count
                FROM knowledge_base_documents member
                JOIN documents document ON document.id = member.document_id
                LEFT JOIN chunks chunk ON chunk.document_id = document.id
                LEFT JOIN claims claim ON claim.document_id = document.id
                WHERE member.knowledge_base_id = %s
                GROUP BY document.id, member.created_at
                ORDER BY member.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (knowledge_base_id, limit, offset),
            )
            rows = cur.fetchall()
    return {
        "knowledge_base_id": knowledge_base_id,
        "documents": rows,
        "limit": limit,
        "offset": offset,
    }


@app.get("/knowledge-bases/{knowledge_base_id}/batches")
def list_ingestion_batches(
    knowledge_base_id: int,
    limit: int = Query(50, ge=1, le=500),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                """
                SELECT
                    id,
                    name,
                    status,
                    submitted_count,
                    accepted_count,
                    error,
                    created_at,
                    updated_at
                FROM ingestion_batches
                WHERE knowledge_base_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (knowledge_base_id, limit),
            )
            rows = cur.fetchall()
    return {
        "knowledge_base_id": knowledge_base_id,
        "batches": rows,
    }


@app.get("/ingestion-batches/{batch_id}")
def get_ingestion_batch(batch_id: int):
    return _batch_payload(batch_id)


@app.get("/knowledge-bases/{knowledge_base_id}/claims")
def get_knowledge_base_claims(
    knowledge_base_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    predicate: str | None = Query(None),
    entity_type: str | None = Query(None),
):
    filters = ["member.knowledge_base_id = %s"]
    params: list[Any] = [knowledge_base_id]
    if predicate:
        filters.append("claim.predicate = %s")
        params.append(normalize_predicate(predicate))
    if entity_type:
        canonical_type = normalize_entity_type(entity_type)
        filters.append(
            "(claim.subject_type = %s OR claim.object_type = %s)"
        )
        params.extend([canonical_type, canonical_type])

    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                f"""
                SELECT
                    claim.id,
                    claim.document_id,
                    document.filename,
                    claim.chunk_id,
                    chunk.section_title,
                    claim.subject_text,
                    claim.subject_type,
                    claim.subject_normalized_id,
                    claim.subject_entity_id,
                    claim.predicate,
                    claim.object_text,
                    claim.object_type,
                    claim.object_normalized_id,
                    claim.object_entity_id,
                    claim.evidence_text,
                    claim.confidence,
                    claim.negated,
                    claim.speculative,
                    claim.claim_json
                FROM claims claim
                JOIN knowledge_base_documents member
                  ON member.document_id = claim.document_id
                JOIN documents document ON document.id = claim.document_id
                LEFT JOIN chunks chunk ON chunk.id = claim.chunk_id
                WHERE {" AND ".join(filters)}
                ORDER BY claim.id
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = cur.fetchall()

    return {
        "knowledge_base_id": knowledge_base_id,
        "claims": rows,
        "limit": limit,
        "offset": offset,
    }


@app.get("/knowledge-bases/{knowledge_base_id}/entities")
def search_knowledge_base_entities(
    knowledge_base_id: int,
    query: str | None = Query(None, max_length=200),
    entity_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    filters = []
    params: list[Any] = []
    if query:
        filters.append(
            """
            (
                entity.canonical_name ILIKE %s
                OR entity.normalized_id ILIKE %s
                OR EXISTS (
                    SELECT 1
                    FROM entity_aliases alias
                    WHERE alias.entity_id = entity.id
                      AND alias.alias ILIKE %s
                )
            )
            """
        )
        pattern = f"%{query.strip()}%"
        params.extend([pattern, pattern, pattern])
    if entity_type:
        filters.append("entity.entity_type = %s")
        params.append(normalize_entity_type(entity_type))

    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                f"""
                WITH scoped_entity_claims AS (
                    SELECT
                        claim.subject_entity_id AS entity_id,
                        claim.id AS claim_id,
                        claim.document_id
                    FROM claims claim
                    JOIN knowledge_base_documents member
                      ON member.document_id = claim.document_id
                    WHERE member.knowledge_base_id = %s
                      AND claim.subject_entity_id IS NOT NULL

                    UNION ALL

                    SELECT
                        claim.object_entity_id AS entity_id,
                        claim.id AS claim_id,
                        claim.document_id
                    FROM claims claim
                    JOIN knowledge_base_documents member
                      ON member.document_id = claim.document_id
                    WHERE member.knowledge_base_id = %s
                      AND claim.object_entity_id IS NOT NULL
                )
                SELECT
                    entity.id,
                    entity.canonical_name,
                    entity.entity_type,
                    entity.normalized_id,
                    ARRAY_AGG(DISTINCT alias.alias)
                        FILTER (WHERE alias.alias IS NOT NULL) AS aliases,
                    COUNT(DISTINCT scoped.document_id)::INT AS document_count,
                    COUNT(DISTINCT scoped.claim_id)::INT AS evidence_count
                FROM entities entity
                JOIN scoped_entity_claims scoped
                  ON scoped.entity_id = entity.id
                LEFT JOIN entity_aliases alias ON alias.entity_id = entity.id
                {"WHERE " + " AND ".join(filters) if filters else ""}
                GROUP BY entity.id
                ORDER BY document_count DESC, entity.canonical_name
                LIMIT %s OFFSET %s
                """,
                (
                    knowledge_base_id,
                    knowledge_base_id,
                    *params,
                    limit,
                    offset,
                ),
            )
            rows = cur.fetchall()

    return {
        "knowledge_base_id": knowledge_base_id,
        "entities": rows,
        "limit": limit,
        "offset": offset,
    }


@app.get("/knowledge-bases/{knowledge_base_id}/graph")
def get_knowledge_base_graph(
    knowledge_base_id: int,
    limit: int = Query(300, ge=1, le=2000),
    min_document_count: int = Query(1, ge=1),
    evidence_limit: int = Query(3, ge=1, le=20),
    predicate: str | None = Query(None),
    entity_type: str | None = Query(None),
    include_negated: bool = Query(True),
    include_speculative: bool = Query(True),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            graph = fetch_graph(
                cur,
                knowledge_base_id=knowledge_base_id,
                limit=limit,
                min_document_count=min_document_count,
                evidence_limit=evidence_limit,
                predicate=predicate,
                entity_type=entity_type,
                include_negated=include_negated,
                include_speculative=include_speculative,
            )
    graph["knowledge_base_id"] = knowledge_base_id
    return graph


@app.post(
    "/knowledge-bases/{knowledge_base_id}/rebuild-graph",
    status_code=202,
)
def rebuild_graph_projection(knowledge_base_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
    result = rebuild_knowledge_graph.apply_async(
        args=[knowledge_base_id],
        queue="extract",
    )
    return {
        "knowledge_base_id": knowledge_base_id,
        "task_id": result.id,
        "status": "queued",
    }


# Backward-compatible single-document endpoint. It now creates a one-item batch
# in the default knowledge base.
@app.post("/documents", status_code=202)
def upload_document(file: UploadFile = File(...)):
    knowledge_base_id = _get_default_knowledge_base_id()
    batch = _ingest_files(
        knowledge_base_id=knowledge_base_id,
        files=[file],
        batch_name=f"Single upload: {safe_filename(file.filename)}",
        force_reprocess=False,
    )
    item = batch["items"][0]
    if item["document_id"] is None:
        raise HTTPException(
            status_code=400,
            detail=item["error"] or "The PDF was rejected",
        )
    return {
        "document_id": item["document_id"],
        "sha256": item["sha256"],
        "status": item["status"],
        "batch_id": batch["id"],
        "knowledge_base_id": knowledge_base_id,
        "was_new_document": item.get("was_new_document", False),
        "queued_for_processing": item.get("queued_for_processing", False),
        "error": item["error"],
    }


@app.get("/documents")
def list_documents(limit: int = Query(50, ge=1, le=500)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    document.id,
                    document.filename,
                    document.title,
                    document.status,
                    document.error,
                    document.created_at,
                    document.updated_at,
                    COUNT(DISTINCT chunk.id)::INT AS chunk_count,
                    COUNT(DISTINCT chunk.id) FILTER (
                        WHERE chunk.status = 'done'
                    )::INT AS chunk_done_count,
                    COUNT(DISTINCT chunk.id) FILTER (
                        WHERE chunk.status = 'failed'
                    )::INT AS chunk_failed_count,
                    COUNT(DISTINCT claim.id)::INT AS claim_count
                FROM documents document
                LEFT JOIN chunks chunk ON chunk.document_id = document.id
                LEFT JOIN claims claim ON claim.document_id = document.id
                GROUP BY document.id
                ORDER BY document.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return {"documents": rows}


@app.get("/documents/{document_id}")
def get_document(document_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    sha256,
                    filename,
                    title,
                    status,
                    error,
                    created_at,
                    updated_at
                FROM documents
                WHERE id = %s
                """,
                (document_id,),
            )
            document = cur.fetchone()
            if not document:
                raise HTTPException(status_code=404, detail="Document not found")

            cur.execute(
                """
                SELECT status, COUNT(*)::INT AS count
                FROM chunks
                WHERE document_id = %s
                GROUP BY status
                ORDER BY status
                """,
                (document_id,),
            )
            chunk_stats = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*)::INT AS count FROM claims WHERE document_id = %s",
                (document_id,),
            )
            claim_count = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT kb.id, kb.slug, kb.name
                FROM knowledge_bases kb
                JOIN knowledge_base_documents member
                  ON member.knowledge_base_id = kb.id
                WHERE member.document_id = %s
                ORDER BY kb.id
                """,
                (document_id,),
            )
            knowledge_bases = cur.fetchall()

    return {
        "document": document,
        "chunks": chunk_stats,
        "claim_count": claim_count,
        "knowledge_bases": knowledge_bases,
    }


@app.get("/documents/{document_id}/claims")
def get_claims(
    document_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    subject_text,
                    subject_type,
                    subject_normalized_id,
                    subject_entity_id,
                    predicate,
                    object_text,
                    object_type,
                    object_normalized_id,
                    object_entity_id,
                    evidence_text,
                    confidence,
                    negated,
                    speculative,
                    claim_json
                FROM claims
                WHERE document_id = %s
                ORDER BY id
                LIMIT %s OFFSET %s
                """,
                (document_id, limit, offset),
            )
            rows = cur.fetchall()
    return {
        "document_id": document_id,
        "claims": rows,
        "limit": limit,
        "offset": offset,
    }


@app.get("/documents/{document_id}/graph")
def get_document_graph(
    document_id: int,
    limit: int = Query(300, ge=1, le=2000),
    evidence_limit: int = Query(3, ge=1, le=20),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM documents WHERE id = %s",
                (document_id,),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Document not found")
            graph = fetch_graph(
                cur,
                document_id=document_id,
                limit=limit,
                evidence_limit=evidence_limit,
            )
    graph["document_id"] = document_id
    return graph
