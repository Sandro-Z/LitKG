from app.batch_status import sync_batches_for_document
from app.celery_app import celery_app
from app.db import get_conn
from app.graph_store import project_existing_claim, store_claim
from app.llm import LLM_MODEL, extract_claims
from app.parser import parse_pdf_to_chunks


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

        title, parsed_chunks = parse_pdf_to_chunks(document["source_path"])

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE documents
                    SET title = %s, status = 'chunked', error = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (title, document_id),
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
                            text,
                            token_estimate,
                            status
                        )
                        VALUES (%s, %s, %s, %s, %s, 'queued')
                        ON CONFLICT (document_id, chunk_index)
                        DO UPDATE SET
                            section_title = EXCLUDED.section_title,
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
                        id,
                        document_id,
                        section_title,
                        text
                    FROM chunks
                    WHERE id = %s
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
                # Makes Celery redelivery and manual reprocessing idempotent.
                cur.execute("DELETE FROM claims WHERE chunk_id = %s", (chunk_id,))

                for claim in bundle.claims:
                    store_claim(
                        cur,
                        document_id=document_id,
                        chunk_id=chunk_id,
                        claim=claim,
                        extraction_model=LLM_MODEL,
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


@celery_app.task(name="rebuild_knowledge_graph", queue="extract")
def rebuild_knowledge_graph(knowledge_base_id: int):
    last_claim_id = 0
    projected_count = 0
    failed_count = 0
    failure_samples = []

    while True:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.claim_json
                    FROM claims c
                    JOIN knowledge_base_documents member
                      ON member.document_id = c.document_id
                    WHERE member.knowledge_base_id = %s
                      AND c.id > %s
                    ORDER BY c.id
                    LIMIT 500
                    """,
                    (knowledge_base_id, last_claim_id),
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

    return {
        "knowledge_base_id": knowledge_base_id,
        "projected_count": projected_count,
        "failed_count": failed_count,
        "failure_samples": failure_samples,
    }
