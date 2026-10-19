from app.db import get_conn
from app.recovery import reconcile_stalled
from psycopg.errors import DeadlockDetected
import time


ACTIVE_STATUSES = ("uploaded", "queued", "parsing", "chunked", "cataloging", "extracting")


def quality_columns(document='document'):
    """Quality counters are independent of terminal processing status."""
    filters = {
        'rejected_candidates': "kind IN ('catalog_rejection','claim_rejection')",
        'excluded_candidates': "kind IN ('catalog_rejection','claim_rejection') AND status='dismissed'",
        'repair_count': "kind IN ('catalog_repair','claim_repair')",
        'pending_reviews': "status='pending'",
    }
    return ', '.join(f"""(SELECT count(*)::INT FROM review_items quality
        WHERE quality.document_id={document}.id
          AND quality.version=COALESCE({document}.catalog_version,'legacy') AND {condition}) AS {name}"""
        for name, condition in filters.items())


def sync_batch_status(cur, batch_id: int) -> None:
    # API callers own their transaction. A failed statement must be rolled
    # back to a savepoint before retrying on that same connection.
    for attempt in range(3):
        try:
            with cur.connection.transaction():
                _sync_batch_status(cur, batch_id)
            return
        except DeadlockDetected:
            if attempt == 2:
                raise
            time.sleep(.05 * (attempt + 1))


def _sync_batch_status(cur, batch_id: int) -> None:
    # Serialize all item/snapshot writers for a batch before touching items.
    # Worker transactions visit batches in ascending order below.
    cur.execute('SELECT id FROM ingestion_batches WHERE id=%s FOR NO KEY UPDATE', (batch_id,))
    reconcile_stalled(cur, batch_id=batch_id)
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
          AND (item.status, item.error) IS DISTINCT FROM (document.status, document.error)
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
        ), snapshot AS (
            SELECT stats.*, CASE
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
            END AS status FROM stats
        )
        UPDATE ingestion_batches batch
        SET
            submitted_count = snapshot.submitted_count,
            accepted_count = snapshot.accepted_count,
            status = snapshot.status,
            updated_at = now()
        FROM snapshot
        WHERE batch.id = snapshot.id
          AND (batch.submitted_count, batch.accepted_count, batch.status)
              IS DISTINCT FROM (snapshot.submitted_count, snapshot.accepted_count, snapshot.status)
        """,
        (list(ACTIVE_STATUSES), batch_id),
    )


def sync_batches_for_document(document_id: int) -> None:
    for attempt in range(3):
        try:
            # Exit the entire connection context (rollback/close) on deadlock;
            # never retry an aborted transaction or retain earlier batch locks.
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT DISTINCT batch_id FROM ingestion_batch_items
                        WHERE document_id = %s ORDER BY batch_id""", (document_id,))
                    batch_ids = sorted(row['batch_id'] for row in cur.fetchall())
                    for batch_id in batch_ids:
                        _sync_batch_status(cur, batch_id)
            return
        except DeadlockDetected:
            if attempt == 2:
                raise
            time.sleep(.05 * (attempt + 1))
