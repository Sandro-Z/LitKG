"""Read-only visibility into extraction-time external background research."""
from fastapi import HTTPException, Query
from app.db import get_conn
from app.web_research import status

def web_research_status():
    return status()


def document_web_research(document_id: int, limit: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        if not conn.execute('SELECT id FROM documents WHERE id=%s', (document_id,)).fetchone():
            raise HTTPException(404, 'Document not found')
        rows = conn.execute('''SELECT id, chunk_id, stage, version, status, created_at,
            COALESCE(output->'parsed'->'_web_research', output->'web_research', '[]'::jsonb) AS research
            FROM processing_attempts WHERE document_id=%s
            AND (jsonb_array_length(COALESCE(output->'parsed'->'_web_research','[]'::jsonb)) > 0
              OR jsonb_array_length(COALESCE(output->'web_research','[]'::jsonb)) > 0)
            ORDER BY id DESC LIMIT %s''', (document_id, limit)).fetchall()
    return {'document_id': document_id, 'attempts': rows, 'limit': limit,
            'evidence_scope': 'pdf_only', 'identifier_candidates_applied': False}
