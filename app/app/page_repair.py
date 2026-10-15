"""Repair saved page metadata from local PDFs without re-extracting claims.

Run inside the API container, for example:
    python -m app.page_repair --knowledge-base-id 1 --dry-run
"""

import argparse
import json

from psycopg.errors import LockNotAvailable
from psycopg.types.json import Jsonb

from app.db import get_conn
from app.page_mapping import PdfPageIndex
from app.parser import fallback_pdf_text
from app.provenance import locate_evidence_pages, page_bounds


def repair_document_pages(document_id: int, *, dry_run=False):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path, status FROM documents WHERE id = %s FOR UPDATE NOWAIT",
                    (document_id,),
                )
                document = cur.fetchone()
                if not document:
                    return {"document_id": document_id, "status": "missing"}
                if document["status"] in {"queued", "parsing", "chunked", "extracting"}:
                    return {"document_id": document_id, "status": "skipped_active"}
                cur.execute(
                    "SELECT id, text, status, page_start, page_end, page_spans FROM chunks "
                    "WHERE document_id = %s ORDER BY chunk_index FOR UPDATE NOWAIT",
                    (document_id,),
                )
                chunks = cur.fetchall()
                if any(chunk["status"] in {"queued", "extracting"} for chunk in chunks):
                    return {"document_id": document_id, "status": "skipped_active"}
                _, pages = fallback_pdf_text(document["source_path"])
                if not pages:
                    return {"document_id": document_id, "status": "no_pdf_text"}
                index = PdfPageIndex.from_sections(pages)
                mapped_chunks = {}
                changed_chunks = 0
                for chunk in chunks:
                    spans = index.page_spans(chunk["text"], chunk["page_spans"])
                    first, last = page_bounds(spans)
                    mapped_chunks[chunk["id"]] = {
                        **chunk, "page_start": first, "page_end": last, "page_spans": spans,
                    }
                    if (first, last, spans) != (chunk["page_start"], chunk["page_end"], chunk["page_spans"]):
                        changed_chunks += 1
                        if not dry_run:
                            cur.execute(
                                "UPDATE chunks SET page_start = %s, page_end = %s, page_spans = %s, "
                                "updated_at = now() WHERE id = %s",
                                (first, last, Jsonb(spans), chunk["id"]),
                            )
                cur.execute(
                    "SELECT id, chunk_id, evidence_text, claim_json FROM claims "
                    "WHERE document_id = %s ORDER BY id FOR UPDATE NOWAIT",
                    (document_id,),
                )
                changed_claims = located_claims = unresolved_claims = 0
                for claim in cur.fetchall():
                    original = claim["claim_json"]
                    evidence = original.get("evidence") or {}
                    sentence = evidence.get("sentence") or claim["evidence_text"]
                    mapped_chunk = mapped_chunks[claim["chunk_id"]]
                    bounds = page_bounds([mapped_chunk])
                    first, last = index.locate(sentence, within=bounds if bounds[0] else None)
                    if first is None:
                        first, last = index.locate(sentence)
                    if first is None:
                        first, last = locate_evidence_pages(sentence, mapped_chunk)
                    if first is None:
                        unresolved_claims += 1
                        # An unmatched sentence is not evidence that an existing
                        # saved location is wrong. Leave it untouched and report it.
                        continue
                    located_claims += 1
                    updated = {**original, "evidence": {**evidence, "page": first, "page_end": last}}
                    if updated != original:
                        changed_claims += 1
                        if not dry_run:
                            cur.execute("UPDATE claims SET claim_json = %s WHERE id = %s",
                                        (Jsonb(updated), claim["id"]))
                return {
                    "document_id": document_id, "status": "preview" if dry_run else "done",
                    "changed_chunk_count": changed_chunks, "changed_claim_count": changed_claims,
                    "located_claim_count": located_claims, "unresolved_claim_count": unresolved_claims,
                }
    except LockNotAvailable:
        return {"document_id": document_id, "status": "skipped_active"}


def repair_pages(*, knowledge_base_id=None, document_id=None, dry_run=False):
    if (knowledge_base_id is None) == (document_id is None):
        raise ValueError("Select exactly one knowledge base or document")
    selected_id = document_id if knowledge_base_id is None else knowledge_base_id
    if selected_id <= 0:
        raise ValueError("IDs must be positive")
    with get_conn() as conn:
        if knowledge_base_id is not None:
            if not conn.execute("SELECT id FROM knowledge_bases WHERE id = %s", (knowledge_base_id,)).fetchone():
                raise ValueError("Knowledge base not found")
            document_ids = [row["document_id"] for row in conn.execute(
                "SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id = %s ORDER BY document_id",
                (knowledge_base_id,),
            )]
        else:
            document_ids = [document_id]
    results = []
    for selected_id in document_ids:
        try:
            results.append(repair_document_pages(selected_id, dry_run=dry_run))
        except Exception as exc:
            # Paths or database connection details may occur in exception text.
            results.append({"document_id": selected_id, "status": "failed", "error_type": type(exc).__name__})
    return {"dry_run": dry_run, "documents": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--knowledge-base-id", type=int)
    scope.add_argument("--document-id", type=int)
    parser.add_argument("--dry-run", action="store_true", help="Preview counts without changing saved data")
    args = parser.parse_args()
    report = repair_pages(**vars(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(row["status"] in {"failed", "missing", "no_pdf_text"} for row in report["documents"]))


if __name__ == "__main__":
    raise SystemExit(main())
