from psycopg.types.json import Jsonb

from app.batch_status import sync_batches_for_document
from app.celery_app import celery_app
from app.db import get_conn
from app.graph_diagnostics import fetch_graph_diagnostics
from app.graph_store import lock_graph_projection, project_existing_claim, store_claim
from app.llm import LLM_MODEL, extract_claims
from app.normalization import extract_abbreviations
from app.parser import parse_pdf_to_chunks
from app.provenance import locate_evidence_pages


def update_document_status(
    document_id: int,
    status: str,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET status = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, error, document_id),
            )
    sync_batches_for_document(document_id)


def update_chunk_status(
    chunk_id: int,
    status: str,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE chunks
                SET status = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, error, chunk_id),
            )


def refresh_document_status(document_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)::INT AS total,
                    COUNT(*) FILTER (WHERE status = 'done')::INT AS done,
                    COUNT(*) FILTER (WHERE status = 'failed')::INT AS failed,
                    COUNT(*) FILTER (
                        WHERE status IN ('queued', 'extracting')
                    )::INT AS active,
                    STRING_AGG(error, '; ' ORDER BY id)
                        FILTER (WHERE status = 'failed' AND error IS NOT NULL)
                        AS errors
                FROM chunks
                WHERE document_id = %s
                """,
                (document_id,),
            )
            stats = cur.fetchone()

            if stats["total"] == 0:
                status = "failed"
                error = "No chunks were generated"
            elif stats["active"] > 0:
                status = "extracting"
                error = None
            elif stats["failed"] == 0:
                status = "done"
                error = None
            elif stats["done"] == 0:
                status = "failed"
                error = stats["errors"]
            else:
                status = "partial"
                error = stats["errors"]

            cur.execute(
                """
                UPDATE documents
                SET status = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, error, document_id),
            )

    sync_batches_for_document(document_id)
    return status


@celery_app.task(name="parse_document", queue="parse")
def parse_document(document_id: int):
    try:
        update_document_status(document_id, "parsing")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path FROM documents WHERE id = %s",
                    (document_id,),
                )
                document = cur.fetchone()

        if not document:
            raise RuntimeError(f"Document not found: {document_id}")

        metadata, parsed_chunks = parse_pdf_to_chunks(document["source_path"])
        abbreviations = extract_abbreviations("\n".join(chunk["text"] for chunk in parsed_chunks))

        with get_conn() as conn:
            with conn.cursor() as cur:
                lock_graph_projection(cur)
                cur.execute(
                    """
                    UPDATE documents
                    SET title = COALESCE(%s, title), doi = COALESCE(%s, doi),
                        abbreviation_map = %s,
                        status = 'chunked', error = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (metadata["title"], metadata["doi"], Jsonb(abbreviations), document_id),
                )

                # A re-run replaces the document's projection. Evidence from other
                # documents remains linked to the shared relations.
                cur.execute(
                    "DELETE FROM claims WHERE document_id = %s",
                    (document_id,),
                )

                chunk_ids = []
                for index, chunk in enumerate(parsed_chunks):
                    cur.execute(
                        """
                        INSERT INTO chunks (
                            document_id,
                            chunk_index,
                            section_title,
                            page_start,
                            page_end,
                            page_spans,
                            text,
                            token_estimate,
                            status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued')
                        ON CONFLICT (document_id, chunk_index)
                        DO UPDATE SET
                            section_title = EXCLUDED.section_title,
                            page_start = EXCLUDED.page_start,
                            page_end = EXCLUDED.page_end,
                            page_spans = EXCLUDED.page_spans,
                            text = EXCLUDED.text,
                            token_estimate = EXCLUDED.token_estimate,
                            status = 'queued',
                            error = NULL,
                            updated_at = now()
                        RETURNING id
                        """,
                        (
                            document_id,
                            index,
                            chunk["section_title"],
                            chunk["page_start"],
                            chunk["page_end"],
                            Jsonb(chunk["page_spans"]),
                            chunk["text"],
                            chunk["token_estimate"],
                        ),
                    )
                    chunk_ids.append(cur.fetchone()["id"])

                cur.execute(
                    """
                    DELETE FROM chunks
                    WHERE document_id = %s
                      AND chunk_index >= %s
                    """,
                    (document_id, len(parsed_chunks)),
                )

        update_document_status(document_id, "extracting")

        # Mark the document before publishing chunk tasks. Otherwise a very fast
        # extraction worker could finish the last chunk and set the document to
        # done, only for this task to overwrite it back to extracting.
        for chunk_id in chunk_ids:
            extract_chunk.apply_async(args=[chunk_id], queue="extract")

        return {
            "document_id": document_id,
            "chunk_count": len(chunk_ids),
        }
    except Exception as exc:
        update_document_status(document_id, "failed", str(exc))
        raise


@celery_app.task(name="extract_chunk", queue="extract")
def extract_chunk(chunk_id: int):
    document_id = None
    try:
        update_chunk_status(chunk_id, "extracting")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        chunk.id,
                        chunk.document_id,
                        chunk.section_title,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.page_spans,
                        chunk.text,
                        document.abbreviation_map
                    FROM chunks chunk
                    JOIN documents document ON document.id = chunk.document_id
                    WHERE chunk.id = %s
                    """,
                    (chunk_id,),
                )
                chunk = cur.fetchone()

        if not chunk:
            raise RuntimeError(f"Chunk not found: {chunk_id}")

        document_id = chunk["document_id"]
        bundle = extract_claims(
            section_title=chunk["section_title"] or "Unknown",
            chunk_text=chunk["text"],
        )

        with get_conn() as conn:
            with conn.cursor() as cur:
                lock_graph_projection(cur)
                # Makes Celery redelivery and manual reprocessing idempotent.
                cur.execute("DELETE FROM claims WHERE chunk_id = %s", (chunk_id,))

                for claim in bundle.claims:
                    page, page_end = locate_evidence_pages(claim.evidence.sentence, chunk)
                    claim.evidence.page = page
                    claim.evidence.page_end = page_end
                    if not claim.evidence.section:
                        claim.evidence.section = chunk["section_title"]
                    store_claim(
                        cur,
                        document_id=document_id,
                        chunk_id=chunk_id,
                        claim=claim,
                        extraction_model=LLM_MODEL,
                        abbreviations=chunk.get("abbreviation_map") or {},
                    )

                cur.execute(
                    """
                    UPDATE chunks
                    SET status = 'done', error = NULL, updated_at = now()
                    WHERE id = %s
                    """,
                    (chunk_id,),
                )

        refresh_document_status(document_id)
        return {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "claim_count": len(bundle.claims),
        }
    except Exception as exc:
        update_chunk_status(chunk_id, "failed", str(exc))
        if document_id is not None:
            refresh_document_status(document_id)
        raise


def _rebuild_knowledge_graph(knowledge_base_id: int):
    last_claim_id = 0
    projected_count = 0
    failed_count = 0
    failure_samples = []
    abbreviations_by_document = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            before = fetch_graph_diagnostics(cur, knowledge_base_id)
            cur.execute(
                """
                SELECT d.id, d.status,
                    EXISTS (SELECT 1 FROM chunks ch WHERE ch.document_id = d.id AND ch.status = 'extracting') AS active_chunks
                FROM documents d
                JOIN knowledge_base_documents member ON member.document_id = d.id
                WHERE member.knowledge_base_id = %s
                ORDER BY d.id
                """,
                (knowledge_base_id,),
            )
            documents = cur.fetchall()
    active_statuses = {"queued", "parsing", "extracting", "chunked"}
    document_ids = [document["id"] for document in documents if document["status"] not in active_statuses and not document["active_chunks"]]
    for document_id in document_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT text FROM chunks WHERE document_id = %s ORDER BY chunk_index", (document_id,))
                abbreviations = extract_abbreviations("\n".join(row["text"] for row in cur.fetchall()))
                abbreviations_by_document[document_id] = abbreviations
                cur.execute("UPDATE documents SET abbreviation_map = %s WHERE id = %s", (Jsonb(abbreviations), document_id))

    while document_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.document_id, c.claim_json
                    FROM claims c
                    JOIN knowledge_base_documents member
                      ON member.document_id = c.document_id
                    WHERE member.knowledge_base_id = %s
                      AND c.id > %s
                      AND c.document_id = ANY(%s::BIGINT[])
                    ORDER BY c.id
                    LIMIT 500
                    """,
                    (knowledge_base_id, last_claim_id, document_ids),
                )
                rows = cur.fetchall()

        if not rows:
            break

        with get_conn() as conn:
            for row in rows:
                last_claim_id = row["id"]
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            project_existing_claim(
                                cur,
                                claim_id=row["id"],
                                claim_json=row["claim_json"],
                                abbreviations=abbreviations_by_document[row["document_id"]],
                                document_id=row["document_id"],
                            )
                    projected_count += 1
                except Exception as exc:
                    failed_count += 1
                    if len(failure_samples) < 20:
                        failure_samples.append(
                            {
                                "claim_id": row["id"],
                                "error": str(exc),
                            }
                        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            after = fetch_graph_diagnostics(cur, knowledge_base_id)
    return {
        "knowledge_base_id": knowledge_base_id,
        "projected_count": projected_count,
        "failed_count": failed_count,
        "failure_samples": failure_samples,
        "skipped_active_document_count": len(documents) - len(document_ids),
        "abbreviation_count": sum(bool(value) for values in abbreviations_by_document.values() for value in values.values()),
        "ambiguous_abbreviation_count": sum(value is None for values in abbreviations_by_document.values() for value in values.values()),
        "before": before,
        "after": after,
    }


def _update_rebuild_job(job_id, knowledge_base_id, status, result=None, error=None):
    if job_id is None:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE graph_rebuild_jobs
                SET status = %s, result = %s, error = %s, updated_at = now()
                WHERE id = %s::UUID AND knowledge_base_id = %s
                """,
                (status, Jsonb(result) if result is not None else None, error, job_id, knowledge_base_id),
            )


@celery_app.task(name="rebuild_knowledge_graph", queue="extract")
def rebuild_knowledge_graph(knowledge_base_id: int, job_id: str | None = None):
    _update_rebuild_job(job_id, knowledge_base_id, "processing")
    try:
        result = _rebuild_knowledge_graph(knowledge_base_id)
        status = "partial" if result["failed_count"] or result["skipped_active_document_count"] else "done"
        _update_rebuild_job(job_id, knowledge_base_id, status, result=result)
        return result
    except Exception as exc:
        _update_rebuild_job(job_id, knowledge_base_id, "failed", error=f"Graph rebuild failed: {type(exc).__name__}")
        raise
