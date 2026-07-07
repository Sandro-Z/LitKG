import hashlib
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Query

from app.db import get_conn
from app.tasks import parse_document


UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LitKG Minimal API")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/documents")
def upload_document(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported in v1")

    tmp_path = UPLOAD_DIR / f"tmp_{file.filename}"

    with tmp_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    digest = sha256_file(tmp_path)
    final_path = UPLOAD_DIR / f"{digest}.pdf"

    if final_path.exists():
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.rename(final_path)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (
                    sha256,
                    filename,
                    source_path,
                    status
                )
                VALUES (%s, %s, %s, 'uploaded')
                ON CONFLICT (sha256)
                DO UPDATE SET
                    filename = EXCLUDED.filename,
                    source_path = EXCLUDED.source_path,
                    updated_at = now()
                RETURNING id, status
                """,
                (digest, file.filename, str(final_path)),
            )
            row = cur.fetchone()
            document_id = row["id"]

    parse_document.apply_async(args=[document_id], queue="parse")

    return {
        "document_id": document_id,
        "sha256": digest,
        "status": "queued",
    }


@app.get("/documents")
def list_documents(limit: int = Query(50, ge=1, le=500)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.id,
                    d.filename,
                    d.title,
                    d.status,
                    d.error,
                    d.created_at,
                    d.updated_at,
                    COUNT(c.id) AS chunk_count,
                    COUNT(c.id) FILTER (WHERE c.status = 'done') AS chunk_done_count,
                    COUNT(c.id) FILTER (WHERE c.status = 'failed') AS chunk_failed_count,
                    COUNT(cl.id) AS claim_count
                FROM documents d
                LEFT JOIN chunks c ON c.document_id = d.id
                LEFT JOIN claims cl ON cl.document_id = d.id
                GROUP BY d.id
                ORDER BY d.created_at DESC
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
            doc = cur.fetchone()

            if not doc:
                raise HTTPException(status_code=404, detail="Document not found")

            cur.execute(
                """
                SELECT status, count(*) AS count
                FROM chunks
                WHERE document_id = %s
                GROUP BY status
                ORDER BY status
                """,
                (document_id,),
            )
            chunk_stats = cur.fetchall()

            cur.execute(
                """
                SELECT count(*) AS count
                FROM claims
                WHERE document_id = %s
                """,
                (document_id,),
            )
            claim_count = cur.fetchone()["count"]

    return {
        "document": doc,
        "chunks": chunk_stats,
        "claim_count": claim_count,
    }


@app.get("/documents/{document_id}/claims")
def get_claims(document_id: int, limit: int = Query(100, ge=1, le=1000)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    subject_text,
                    subject_type,
                    predicate,
                    object_text,
                    object_type,
                    evidence_text,
                    confidence,
                    negated,
                    speculative,
                    claim_json
                FROM claims
                WHERE document_id = %s
                ORDER BY id
                LIMIT %s
                """,
                (document_id, limit),
            )
            rows = cur.fetchall()

    return {
        "document_id": document_id,
        "claims": rows,
    }


@app.get("/documents/{document_id}/graph")
def get_document_graph(
    document_id: int,
    limit: int = Query(300, ge=1, le=2000),
):
    """
    第一版图谱接口：
    直接从 claims 表中构造 node-edge 结构。
    后续可替换为 Neo4j / RDF / NetworkX 后端。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    subject_text,
                    subject_type,
                    predicate,
                    object_text,
                    object_type,
                    evidence_text,
                    confidence,
                    negated,
                    speculative
                FROM claims
                WHERE document_id = %s
                ORDER BY confidence DESC NULLS LAST, id ASC
                LIMIT %s
                """,
                (document_id, limit),
            )
            claims = cur.fetchall()

    node_map = {}
    edges = []

    def node_key(text, typ):
        typ = typ or "Other"
        text = text or "Unknown"
        return f"{typ}:{text}"

    for c in claims:
        s_key = node_key(c["subject_text"], c["subject_type"])
        o_key = node_key(c["object_text"], c["object_type"])

        if s_key not in node_map:
            node_map[s_key] = {
                "id": s_key,
                "label": c["subject_text"] or "Unknown",
                "type": c["subject_type"] or "Other",
            }

        if o_key not in node_map:
            node_map[o_key] = {
                "id": o_key,
                "label": c["object_text"] or "Unknown",
                "type": c["object_type"] or "Other",
            }

        edges.append(
            {
                "id": c["id"],
                "source": s_key,
                "target": o_key,
                "label": c["predicate"],
                "confidence": c["confidence"],
                "negated": c["negated"],
                "speculative": c["speculative"],
                "evidence": c["evidence_text"],
            }
        )

    return {
        "document_id": document_id,
        "nodes": list(node_map.values()),
        "edges": edges,
    }
