from app.db import get_conn


ACTIVE_STATUSES = ("uploaded", "queued", "parsing", "chunked", "extracting")


def sync_batch_status(cur, batch_id: int) -> None:
    cur.execute(
        """
        UPDATE ingestion_batch_items item
        SET
            status = document.status,
            error = document.error,
            updated_at = now()
        FROM documents document
        WHERE item.document_id = document.id
          AND item.batch_id = %s
        """,
        (batch_id,),
    )

    cur.execute(
        """
        WITH stats AS (
            SELECT
                b.id,
                COUNT(item.id)::INT AS submitted_count,
                COUNT(item.document_id)::INT AS accepted_count,
                COUNT(*) FILTER (
                    WHERE item.status = ANY(%s::TEXT[])
                )::INT AS active_count,
                COUNT(*) FILTER (
                    WHERE item.status = 'queued'
                )::INT AS queued_count,
                COUNT(*) FILTER (
                    WHERE item.status = 'done'
                )::INT AS done_count,
                COUNT(*) FILTER (
                    WHERE item.status = 'partial'
                )::INT AS partial_count,
                COUNT(*) FILTER (
                    WHERE item.status IN ('failed', 'rejected')
                )::INT AS failed_count
            FROM ingestion_batches b
            LEFT JOIN ingestion_batch_items item ON item.batch_id = b.id
            WHERE b.id = %s
            GROUP BY b.id
        )
        UPDATE ingestion_batches batch
        SET
            submitted_count = stats.submitted_count,
            accepted_count = stats.accepted_count,
            status = CASE
                WHEN stats.submitted_count = 0 THEN 'failed'
                WHEN stats.accepted_count = 0 THEN 'failed'
                WHEN stats.queued_count = stats.accepted_count
                     AND stats.failed_count = 0 THEN 'queued'
                WHEN stats.active_count > 0 THEN 'processing'
                WHEN stats.done_count = stats.accepted_count
                     AND stats.failed_count = 0 THEN 'done'
                WHEN stats.done_count > 0
                     OR stats.partial_count > 0 THEN 'partial'
                ELSE 'failed'
            END,
            updated_at = now()
        FROM stats
        WHERE batch.id = stats.id
        """,
        (list(ACTIVE_STATUSES), batch_id),
    )


def sync_batches_for_document(document_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT batch_id
                FROM ingestion_batch_items
                WHERE document_id = %s
                """,
                (document_id,),
            )
            batch_ids = [row["batch_id"] for row in cur.fetchall()]
            for batch_id in batch_ids:
                sync_batch_status(cur, batch_id)
