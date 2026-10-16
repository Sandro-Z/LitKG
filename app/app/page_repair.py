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
from app.catalog import archive_claims


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
                if document["status"] in {"queued", "parsing", "chunked", "cataloging", "extracting"}:
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
                if not dry_run:
                    archive_claims(cur, document_id)
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
                    if original.get('additional_evidence'):
                        additional = []
                        for extra in original['additional_evidence']:
                            a,b = index.locate(extra['sentence'],within=bounds if bounds[0] else None)
                            if a is None:
                                a,b = locate_evidence_pages(extra['sentence'],mapped_chunk)
                            additional.append({**extra,'page':a,'page_end':b} if a is not None else extra)
                        updated['additional_evidence'] = additional
                    if updated != original:
                        changed_claims += 1
                        if not dry_run:
                            cur.execute("UPDATE claims SET claim_json = %s WHERE id = %s",
                                        (Jsonb(updated), claim["id"]))
                    if not dry_run:
                        for ordinal, item in enumerate([updated['evidence'],*updated.get('additional_evidence',[])]):
                            cur.execute('''UPDATE fact_evidence SET evidence=%s WHERE ordinal=%s
                                AND fact_id IN (SELECT id FROM facts WHERE claim_id=%s)''',(Jsonb(item),ordinal,claim['id']))
                cur.execute('''SELECT de.catalog_version,de.local_id,de.entry FROM document_entities de
                    JOIN documents d ON d.id=de.document_id WHERE de.document_id=%s AND de.catalog_version=d.catalog_version''',(document_id,))
                changed_directory = 0
                for row in cur.fetchall():
                    entry = row['entry']
                    updated_locations = []
                    for location in entry['locations']:
                        quote = location.get('evidence') or location.get('mention')
                        chunk = mapped_chunks.get(location.get('chunk_id'))
                        if not quote or not chunk or location.get('source_kind','text') != 'text':
                            updated_locations.append(location)
                            continue
                        bounds = page_bounds([chunk])
                        first,last = index.locate(quote,within=bounds if bounds[0] else None)
                        updated_locations.append({**location,'page':first,'page_end':last} if first is not None else location)
                    if updated_locations != entry['locations']:
                        changed_directory += 1
                        if not dry_run:
                            cur.execute('UPDATE document_entities SET entry=%s WHERE document_id=%s AND catalog_version=%s AND local_id=%s',
                                        (Jsonb({**entry,'locations':updated_locations}),document_id,row['catalog_version'],row['local_id']))
                if not dry_run and (changed_chunks or changed_claims or changed_directory):
                    cur.execute('''INSERT INTO derived_dirty_documents (document_id) VALUES (%s)
                        ON CONFLICT (document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1''',(document_id,))
                return {
                    "document_id": document_id, "status": "preview" if dry_run else "done",
                    "changed_chunk_count": changed_chunks, "changed_claim_count": changed_claims,
                    "located_claim_count": located_claims, "unresolved_claim_count": unresolved_claims,
                    'changed_directory_count': changed_directory,
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
    if not args.dry_run:
        from app.tasks import refresh_derived
        for row in report['documents']:
            if row.get('changed_chunk_count') or row.get('changed_claim_count') or row.get('changed_directory_count'):
                try:
                    refresh_derived.apply_async(args=[row['document_id']],queue='extract')
                except Exception as exc:
                    row['derived_error'] = f'Queue unavailable ({type(exc).__name__}); run derived maintenance'
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(row["status"] in {"failed", "missing", "no_pdf_text"} for row in report["documents"]))


if __name__ == "__main__":
    raise SystemExit(main())
