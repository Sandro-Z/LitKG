import os
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)

from app.batch_status import sync_batch_status, quality_columns
from app.db import get_conn
from app.graph_diagnostics import fetch_graph_diagnostics
from app.graph_store import lock_graph_projection, prune_orphan_projection
from app.graph_query import fetch_graph, fetch_relations, substring_pattern, current_claim_clause
from app.ingestion import (
    UploadRejected,
    register_document,
    safe_filename,
    store_pdf,
)
from app.normalization import normalize_entity_type, normalize_predicate
from app.provenance import add_claim_location
from app.schemas import KnowledgeBaseCreate, QuestionRequest, MaintenanceRequest
from app.entity_types import TYPE_DEFINITIONS, TYPE_PARENTS
from app.predicate_registry import REGISTRY_VERSION, PREDICATES, EVIDENCE_CATEGORIES, QUALIFIER_DEFINITIONS
from app.processes import PROCESS_DEFINITIONS
from app.catalog import PROCESSING_VERSION, load_catalog
from app.recovery import reconcile_stalled
from app.retrieval import fetch_facts, retrieve, answer_question
from app.tasks import (
    parse_document,
    rebuild_knowledge_graph,
    update_document_status,
    maintain_knowledge_base,
    refresh_derived,
)


UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
MAX_UPLOAD_BYTES = int(
    os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
MAX_BATCH_FILES = int(os.environ.get("MAX_BATCH_FILES", "200"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="LitKG API",
    version="3.0.0",
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


def _lock_ingestion_knowledge_base(cur, knowledge_base_id: int) -> None:
    # Keep deletion from racing document registration in another transaction.
    cur.execute("SELECT id FROM knowledge_bases WHERE id = %s FOR KEY SHARE",
                (knowledge_base_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Knowledge base not found")


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
                f"""
                SELECT
                    item.id,
                    item.position,
                    item.filename,
                    item.document_id,
                    item.sha256,
                    item.status,
                    item.error,
                    document.title,
                    document.doi,
                    document.created_at AS document_created_at,
                    document.updated_at AS document_updated_at,
                    {quality_columns()}
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
    batch['processing_quality'] = {name: sum(item.get(name, 0) for item in items)
        for name in ('rejected_candidates', 'excluded_candidates', 'repair_count', 'pending_reviews')}
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
            _lock_ingestion_knowledge_base(cur, knowledge_base_id)
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
                    _lock_ingestion_knowledge_base(cur, knowledge_base_id)
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
                    _lock_ingestion_knowledge_base(cur, knowledge_base_id)
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
        except HTTPException:
            raise
        except Exception as exc:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    _lock_ingestion_knowledge_base(cur, knowledge_base_id)
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

    batch = _batch_payload(batch_id, item_metadata=item_metadata)
    for item in batch['items']:
        if item.get('document_id') and item['status'] == 'done':
            try:
                refresh_derived.apply_async(args=[item['document_id']], queue='extract')
            except Exception:
                batch['derived_error'] = 'Could not enqueue derived index refresh; retry derived maintenance'
    return batch


@app.get("/health")
def health():
    return {"status": "ok", "api_version": "3.0.0"}


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


@app.put("/knowledge-bases/{knowledge_base_id}")
def update_knowledge_base(knowledge_base_id: int, payload: KnowledgeBaseCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Knowledge base name cannot be blank")
    description = (payload.description or "").strip() or None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_bases
                SET name = %s, description = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, slug, name, description, created_at, updated_at""",
                (name, description, knowledge_base_id),
            )
            knowledge_base = cur.fetchone()
            if not knowledge_base:
                raise HTTPException(status_code=404, detail="Knowledge base not found")
    return knowledge_base


@app.delete("/knowledge-bases/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: int,
    confirm_name: str = Query(..., min_length=1, max_length=200),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Serialize renames/deletes and verify the name inside the transaction.
            cur.execute(
                "SELECT id, name FROM knowledge_bases WHERE id = %s FOR UPDATE",
                (knowledge_base_id,),
            )
            knowledge_base = cur.fetchone()
            if not knowledge_base:
                raise HTTPException(status_code=404, detail="Knowledge base not found")
            if confirm_name != knowledge_base["name"]:
                raise HTTPException(status_code=409, detail="知识库名称不匹配，请刷新后输入完整名称。")
            # Use the same lock order as extraction: graph projection, then docs.
            lock_graph_projection(cur, True)
            cur.execute("""SELECT d.id FROM documents d
                JOIN knowledge_base_documents m ON m.document_id = d.id
                WHERE m.knowledge_base_id = %s ORDER BY d.id FOR UPDATE OF d""",
                (knowledge_base_id,))
            document_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                "DELETE FROM review_items WHERE detail->>'knowledge_base_id' = %s",
                (str(knowledge_base_id),),
            )
            cur.execute("DELETE FROM knowledge_bases WHERE id = %s", (knowledge_base_id,))
            # Deleting only memberships left the SHA256 and processing caches in
            # place, so uploading the same PDF incorrectly reused completed work.
            cur.execute("""SELECT id FROM documents d WHERE id = ANY(%s::BIGINT[])
                AND NOT EXISTS (SELECT 1 FROM knowledge_base_documents m
                                WHERE m.document_id = d.id) ORDER BY id""", (document_ids,))
            deleted_ids = [row["id"] for row in cur.fetchall()]
            cur.execute("SELECT id FROM chunks WHERE document_id = ANY(%s::BIGINT[])", (deleted_ids,))
            resources = [f"litkg-document:{doc_id}" for doc_id in deleted_ids]
            resources.extend(f"litkg-chunk:{row['id']}" for row in cur.fetchall())
            cur.execute("DELETE FROM processing_leases WHERE resource = ANY(%s::TEXT[])", (resources,))
            cur.execute("DELETE FROM ingestion_batch_items WHERE document_id = ANY(%s::BIGINT[])", (deleted_ids,))
            # Cascades include chunks, catalogs, caches, attempts, claim history,
            # claims, facts, evidence and reviews. Shared documents remain live.
            cur.execute("DELETE FROM documents WHERE id = ANY(%s::BIGINT[])", (deleted_ids,))
            # Maintenance leases use text resource keys rather than foreign keys.
            cur.execute("""DELETE FROM processing_leases lease
                WHERE resource LIKE 'litkg-maintenance:%'
                AND NOT EXISTS (SELECT 1 FROM maintenance_jobs job
                    WHERE lease.resource = 'litkg-maintenance:' || job.id::TEXT)""")
            prune_orphan_projection(cur)
    return {"status": "deleted", "knowledge_base_id": knowledge_base_id,
            "name": knowledge_base["name"], "deleted_document_count": len(deleted_ids),
            "retained_shared_document_count": len(document_ids) - len(deleted_ids)}


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
                f"""
                SELECT
                    document.id,
                    document.sha256,
                    document.filename,
                    document.title,
                    document.doi,
                    document.status,
                    document.error,
                    {quality_columns()},
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
                    document.title,
                    document.doi,
                    claim.chunk_id,
                    chunk.section_title,
                    chunk.page_start AS chunk_page_start,
                    chunk.page_end AS chunk_page_end,
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
            rows = [add_claim_location(row) for row in cur.fetchall()]

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
    if query and query.strip():
        filters.append(
            """
            (
                entity.canonical_name ILIKE %s ESCAPE '!'
                OR entity.normalized_id ILIKE %s ESCAPE '!'
                OR EXISTS (
                    SELECT 1
                    FROM entity_aliases alias
                    WHERE alias.entity_id = entity.id
                      AND alias.alias ILIKE %s ESCAPE '!'
                )
                OR EXISTS (
                    SELECT 1 FROM entity_identifiers identifier
                    WHERE identifier.entity_id = entity.id
                      AND identifier.normalized_id ILIKE %s ESCAPE '!'
                )
            )
            """
        )
        pattern = substring_pattern(query)
        params.extend([pattern, pattern, pattern, pattern])
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
                      AND {current_claim_clause('claim')}

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
                      AND {current_claim_clause('claim')}

                    UNION ALL

                    SELECT participant.entity_id, claim.id AS claim_id, claim.document_id
                    FROM fact_participants participant JOIN facts f ON f.id=participant.fact_id
                    JOIN claims claim ON claim.id=f.claim_id
                    JOIN knowledge_base_documents member ON member.document_id=claim.document_id
                    WHERE member.knowledge_base_id=%s AND {current_claim_clause('claim')}

                    UNION ALL

                    SELECT de.entity_id,NULL::BIGINT,de.document_id FROM document_entities de
                    JOIN documents d ON d.id=de.document_id
                    JOIN knowledge_base_documents member ON member.document_id=de.document_id
                    WHERE member.knowledge_base_id=%s AND de.catalog_version=d.catalog_version
                      AND d.catalog_status='done' AND de.entity_id IS NOT NULL
                )
                SELECT
                    entity.id,
                    entity.canonical_name,
                    entity.entity_type,
                    entity.normalized_id,
                    ARRAY(
                        SELECT identifier.normalized_id
                        FROM entity_identifiers identifier
                        WHERE identifier.entity_id = entity.id
                        ORDER BY identifier.normalized_id
                    ) AS identifiers,
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
                ORDER BY document_count DESC, entity.canonical_name, entity.id
                LIMIT %s OFFSET %s
                """,
                (
                    knowledge_base_id,
                    knowledge_base_id,
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


@app.get("/knowledge-bases/{knowledge_base_id}/relations")
def search_knowledge_base_relations(
    knowledge_base_id: int,
    query: str | None = Query(None, max_length=200),
    entity_id: int | None = Query(None, ge=1),
    subject_entity_id: int | None = Query(None, ge=1),
    object_entity_id: int | None = Query(None, ge=1),
    subject: str | None = Query(None, max_length=200),
    object_query: str | None = Query(None, alias="object", max_length=200),
    direction: Literal["both", "outgoing", "incoming"] = Query("both"),
    predicate: str | None = Query(None, max_length=200),
    entity_type: str | None = Query(None, max_length=200),
    min_document_count: int = Query(1, ge=1),
    include_negated: bool = Query(True),
    include_speculative: bool = Query(True),
    evidence_limit: int = Query(3, ge=1, le=20),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    if direction != "both" and entity_id is None:
        raise HTTPException(
            status_code=422,
            detail="entity_id is required for direction filtering",
        )
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            return fetch_relations(
                cur,
                knowledge_base_id=knowledge_base_id,
                query=query,
                entity_id=entity_id,
                subject_entity_id=subject_entity_id,
                object_entity_id=object_entity_id,
                subject=subject,
                object_query=object_query,
                direction=direction,
                predicate=predicate,
                entity_type=entity_type,
                min_document_count=min_document_count,
                include_negated=include_negated,
                include_speculative=include_speculative,
                evidence_limit=evidence_limit,
                limit=limit,
                offset=offset,
            )


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
    query: str | None = Query(None, max_length=200),
    entity_id: int | None = Query(None, ge=1),
    direction: Literal["both", "outgoing", "incoming"] = Query("both"),
):
    if direction != "both" and entity_id is None:
        raise HTTPException(
            status_code=422,
            detail="entity_id is required for direction filtering",
        )
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
                query=query,
                entity_id=entity_id,
                direction=direction,
            )
    graph["knowledge_base_id"] = knowledge_base_id
    return graph


@app.get("/knowledge-bases/{knowledge_base_id}/graph/diagnostics")
def get_graph_diagnostics(knowledge_base_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            metrics = fetch_graph_diagnostics(cur, knowledge_base_id)
    return {"knowledge_base_id": knowledge_base_id, "scope": "full_knowledge_base", **metrics}


@app.get("/knowledge-bases/{knowledge_base_id}/rebuild-graph/{job_id}")
def get_graph_rebuild_job(knowledge_base_id: int, job_id: uuid.UUID):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                """
                SELECT id, knowledge_base_id, status, result, error, created_at, updated_at
                FROM graph_rebuild_jobs WHERE id = %s AND knowledge_base_id = %s
                """,
                (job_id, knowledge_base_id),
            )
            job = cur.fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Graph rebuild job not found")
    return job


@app.post(
    "/knowledge-bases/{knowledge_base_id}/rebuild-graph",
    status_code=202,
)
def rebuild_graph_projection(knowledge_base_id: int):
    job_id = uuid.uuid4()
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute(
                "INSERT INTO graph_rebuild_jobs (id, knowledge_base_id) VALUES (%s, %s)",
                (job_id, knowledge_base_id),
            )
    try:
        rebuild_knowledge_graph.apply_async(
            args=[knowledge_base_id, str(job_id)], task_id=str(job_id), queue="extract",
        )
    except Exception:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE graph_rebuild_jobs SET status = 'failed', error = 'Could not enqueue graph rebuild', updated_at = now() WHERE id = %s",
                    (job_id,),
                )
        raise HTTPException(status_code=503, detail="Could not enqueue graph rebuild")
    return {
        "knowledge_base_id": knowledge_base_id,
        "task_id": str(job_id),
        "status_url": f"/knowledge-bases/{knowledge_base_id}/rebuild-graph/{job_id}",
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
                f"""
                SELECT
                    document.id,
                    document.filename,
                    document.title,
                    document.doi,
                    document.status,
                    document.error,
                    {quality_columns()},
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
                    doi,
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
            cur.execute("SELECT 1 FROM documents WHERE id = %s", (document_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Document not found")
            cur.execute(
                """
                SELECT
                    claim.id,
                    claim.document_id,
                    document.filename,
                    document.title,
                    document.doi,
                    claim.chunk_id,
                    chunk.section_title,
                    chunk.page_start AS chunk_page_start,
                    chunk.page_end AS chunk_page_end,
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
                JOIN documents document ON document.id = claim.document_id
                LEFT JOIN chunks chunk ON chunk.id = claim.chunk_id
                WHERE claim.document_id = %s
                ORDER BY claim.id
                LIMIT %s OFFSET %s
                """,
                (document_id, limit, offset),
            )
            rows = [add_claim_location(row) for row in cur.fetchall()]
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


@app.get('/entity-types')
def entity_types():
    return {"version": PROCESSING_VERSION, "types": TYPE_DEFINITIONS}


@app.get('/predicate-types')
def predicate_types():
    return {"version": REGISTRY_VERSION, "predicates": PREDICATES,
            "qualifiers": QUALIFIER_DEFINITIONS, "evidence_categories": EVIDENCE_CATEGORIES,
            "process_kinds": PROCESS_DEFINITIONS, "type_parents": TYPE_PARENTS}


def _require_member(cur, knowledge_base_id, document_id):
    _get_knowledge_base(cur, knowledge_base_id)
    cur.execute('SELECT 1 FROM knowledge_base_documents WHERE knowledge_base_id=%s AND document_id=%s', (knowledge_base_id, document_id))
    if not cur.fetchone():
        raise HTTPException(404, 'Document is not in this knowledge base')


def _catalog_quality(cur, document_id, version, catalog_status):
    cur.execute("""SELECT count(*)::INT AS rejected_candidates,
        count(*) FILTER (WHERE status='pending')::INT AS pending_rejections
        FROM review_items WHERE document_id=%s AND version=%s AND kind='catalog_rejection'""",
        (document_id, version))
    counts = cur.fetchone()
    return {**counts, 'status': 'not_complete' if catalog_status != 'done' else
            'needs_review' if counts['pending_rejections'] else
            ('reviewed_with_exclusions' if counts['rejected_candidates'] else 'grounded')}


@app.get('/knowledge-bases/{knowledge_base_id}/documents/{document_id}/catalog')
def document_catalog(knowledge_base_id: int, document_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _require_member(cur, knowledge_base_id, document_id)
            cur.execute('SELECT catalog_version,catalog_status,catalog_error,pipeline_generation FROM documents WHERE id=%s', (document_id,))
            document = cur.fetchone()
            entries = load_catalog(cur, document_id, document['catalog_version']) if document['catalog_version'] else []
            quality = _catalog_quality(cur, document_id, document['catalog_version'], document['catalog_status'])
    return {**document, 'entities': entries, 'catalog_quality': quality}


@app.get('/knowledge-bases/{knowledge_base_id}/documents/{document_id}/processing')
def processing_status(knowledge_base_id: int, document_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _require_member(cur, knowledge_base_id, document_id)
            reconcile_stalled(cur, document_id=document_id)
            cur.execute('SELECT id,status,error,catalog_version,catalog_status,catalog_error,pipeline_generation FROM documents WHERE id=%s', (document_id,))
            document = cur.fetchone()
            quality = _catalog_quality(cur, document_id, document['catalog_version'], document['catalog_status'])
            cur.execute(f'SELECT {quality_columns()} FROM documents document WHERE document.id=%s', (document_id,))
            processing_quality = cur.fetchone()
            cur.execute('SELECT id,chunk_index,status,error,extraction_version FROM chunks WHERE document_id=%s ORDER BY chunk_index', (document_id,))
            chunks = cur.fetchall()
            cur.execute('SELECT id,stage,version,status,error,created_at FROM processing_attempts WHERE document_id=%s ORDER BY id DESC LIMIT 100', (document_id,))
            attempts = cur.fetchall()
    return {'document': document, 'catalog_quality': quality, 'processing_quality': processing_quality, 'chunks': chunks, 'attempts': attempts,
            'upgrade': {'reproject': 'No LLM; rebuilds facts/index/summaries from saved claims; cannot recover unrecorded semantics',
                        'reextract': 'LLM required; full saved text directory followed by all chunks',
                        'retry': 'Resumes failed stages with validated segment caches',
                        'derived': 'Rebuilds index and extractive summaries; optional embeddings'}}


@app.get('/knowledge-bases/{knowledge_base_id}/facts')
def complete_facts(knowledge_base_id: int, document_id: int | None = Query(None, ge=1),
                   entity_id: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            rows = fetch_facts(cur, knowledge_base_id, document_id=document_id, entity_id=entity_id, limit=limit, offset=offset)
    for row in rows:
        row.pop('source_text', None)
    return {'facts': rows, 'limit': limit, 'offset': offset}


@app.post('/knowledge-bases/{knowledge_base_id}/ask')
def ask(knowledge_base_id: int, request: QuestionRequest):
    if not request.question.strip():
        raise HTTPException(422, 'Question cannot be blank')
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            result = retrieve(cur, knowledge_base_id, request.question, request.limit, request.hops)
    return answer_question(result, request.question, request.generate)


@app.get('/knowledge-bases/{knowledge_base_id}/browse')
def browse(knowledge_base_id: int, kind: Literal['overview','document','entity','fact'] = Query('overview'),
           source_id: int | None = Query(None, ge=0), query: str | None = Query(None, max_length=200),
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute("""SELECT kind,source_id,content,sources,content_hash,processing_version,updated_at
                FROM derived_content WHERE knowledge_base_id=%s AND kind=%s AND (%s::BIGINT IS NULL OR source_id=%s)
                AND (%s::TEXT IS NULL OR content ILIKE %s ESCAPE '!') ORDER BY source_id LIMIT %s OFFSET %s""",
                (knowledge_base_id,kind,source_id,source_id,query,substring_pattern(query or ''),limit,offset))
            rows = cur.fetchall()
    return {'pages': rows, 'limit': limit, 'offset': offset, 'derived': True}


@app.get('/knowledge-bases/{knowledge_base_id}/reviews')
def reviews(knowledge_base_id: int, status: Literal['pending','resolved','dismissed'] = Query('pending'),
            limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            cur.execute("""SELECT r.* FROM review_items r JOIN documents d ON d.id=r.document_id
                JOIN knowledge_base_documents m ON m.document_id=r.document_id
                WHERE m.knowledge_base_id=%s AND r.status=%s AND r.version=COALESCE(d.catalog_version,'legacy')
                AND (NOT (r.detail ? 'knowledge_base_id') OR r.detail->>'knowledge_base_id'=%s)
                ORDER BY r.id LIMIT %s OFFSET %s""", (knowledge_base_id,status,str(knowledge_base_id),limit,offset))
            rows = cur.fetchall()
    return {'reviews': rows, 'limit': limit, 'offset': offset}


@app.post('/knowledge-bases/{knowledge_base_id}/reviews/{review_id}')
def review_action(knowledge_base_id: int, review_id: int, status: Literal['pending','resolved','dismissed'], note: str = Query('', max_length=2000)):
    with get_conn() as conn:
        row = conn.execute("""UPDATE review_items r SET status=%s,note=%s,updated_at=now(),
            detail=jsonb_set(r.detail,'{review_history}',COALESCE(r.detail->'review_history','[]'::jsonb) ||
                jsonb_build_array(jsonb_build_object('from',r.status,'to',%s::TEXT,'note',%s::TEXT,'at',now())))
            WHERE r.id=%s AND EXISTS (SELECT 1 FROM knowledge_base_documents m WHERE m.document_id=r.document_id AND m.knowledge_base_id=%s)
            AND (NOT (r.detail ? 'knowledge_base_id') OR r.detail->>'knowledge_base_id'=%s) RETURNING id,status,note""",
            (status,note,status,note,review_id,knowledge_base_id,str(knowledge_base_id))).fetchone()
    if not row:
        raise HTTPException(404, 'Review item not found')
    return row


@app.post('/knowledge-bases/{knowledge_base_id}/maintenance', status_code=202)
def start_maintenance(knowledge_base_id: int, request: MaintenanceRequest):
    job_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_knowledge_base(cur, knowledge_base_id)
            if request.document_id:
                _require_member(cur, knowledge_base_id, request.document_id)
            cur.execute('INSERT INTO maintenance_jobs (id,knowledge_base_id,document_id,mode) VALUES (%s::UUID,%s,%s,%s)',
                        (job_id,knowledge_base_id,request.document_id,request.mode))
    try:
        maintain_knowledge_base.apply_async(args=[job_id], queue='extract', task_id=job_id)
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("UPDATE maintenance_jobs SET status='failed',error=%s WHERE id=%s::UUID", (f'Queue publication failed ({type(exc).__name__})',job_id))
        raise HTTPException(503, f'Queue unavailable; failed job {job_id} is recorded') from None
    return {'task_id': job_id, 'mode': request.mode, 'llm_required': request.mode in {'reextract','retry'},
            'status_url': f'/knowledge-bases/{knowledge_base_id}/maintenance/{job_id}',
            'scope': 'Shared documents are updated in every knowledge base containing them'}


@app.get('/knowledge-bases/{knowledge_base_id}/maintenance/{job_id}')
def maintenance_status(knowledge_base_id: int, job_id: uuid.UUID):
    with get_conn() as conn:
        row = conn.execute('SELECT * FROM maintenance_jobs WHERE id=%s AND knowledge_base_id=%s', (job_id,knowledge_base_id)).fetchone()
    if not row:
        raise HTTPException(404, 'Maintenance job not found')
    return row
