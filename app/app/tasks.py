from psycopg.types.json import Jsonb
from app.celery_app import celery_app
from app.db import get_conn
from app.parser import parse_pdf_to_chunks
from app.llm import extract_claims, LLM_MODEL


def update_document_status(document_id: int, status: str, error: str | None = None):
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


def update_chunk_status(chunk_id: int, status: str, error: str | None = None):
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
                doc = cur.fetchone()

        if not doc:
            raise RuntimeError(f"Document not found: {document_id}")

        title, chunks = parse_pdf_to_chunks(doc["source_path"])

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE documents
                    SET title = %s, status = 'chunked', updated_at = now()
                    WHERE id = %s
                    """,
                    (title, document_id),
                )

                chunk_ids = []
                for idx, chunk in enumerate(chunks):
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
                            idx,
                            chunk["section_title"],
                            chunk["text"],
                            chunk["token_estimate"],
                        ),
                    )
                    row = cur.fetchone()
                    chunk_ids.append(row["id"])

        for chunk_id in chunk_ids:
            extract_chunk.apply_async(args=[chunk_id], queue="extract")

        update_document_status(document_id, "extracting")

    except Exception as e:
        update_document_status(document_id, "failed", str(e))
        raise


@celery_app.task(name="extract_chunk", queue="extract")
def extract_chunk(chunk_id: int):
    try:
        update_chunk_status(chunk_id, "extracting")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.id,
                        c.document_id,
                        c.section_title,
                        c.text
                    FROM chunks c
                    WHERE c.id = %s
                    """,
                    (chunk_id,),
                )
                chunk = cur.fetchone()

        if not chunk:
            raise RuntimeError(f"Chunk not found: {chunk_id}")

        bundle = extract_claims(
            section_title=chunk["section_title"] or "Unknown",
            chunk_text=chunk["text"],
        )

        with get_conn() as conn:
            with conn.cursor() as cur:
                for claim in bundle.claims:
                    cj = claim.model_dump()

                    cur.execute(
                        """
                        INSERT INTO claims (
                            document_id,
                            chunk_id,
                            subject_text,
                            subject_type,
                            predicate,
                            object_text,
                            object_type,
                            evidence_text,
                            confidence,
                            negated,
                            speculative,
                            claim_json,
                            extraction_model
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            chunk["document_id"],
                            chunk_id,
                            claim.subject.text,
                            claim.subject.type,
                            claim.predicate,
                            claim.object.text,
                            claim.object.type,
                            claim.evidence.sentence,
                            claim.confidence,
                            claim.negated,
                            claim.speculative,
                            Jsonb(cj),
                            LLM_MODEL,
                        ),
                    )

                cur.execute(
                    """
                    UPDATE chunks
                    SET status = 'done', error = NULL, updated_at = now()
                    WHERE id = %s
                    """,
                    (chunk_id,),
                )

                cur.execute(
                    """
                    UPDATE documents
                    SET status = CASE
                        WHEN NOT EXISTS (
                            SELECT 1 FROM chunks
                            WHERE document_id = %s
                            AND status NOT IN ('done', 'failed')
                        )
                        THEN 'done'
                        ELSE status
                    END,
                    updated_at = now()
                    WHERE id = %s
                    """,
                    (chunk["document_id"], chunk["document_id"]),
                )

    except Exception as e:
        update_chunk_status(chunk_id, "failed", str(e))
        raise
