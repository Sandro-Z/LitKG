import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from app.recovery import reconcile_stalled


class UploadRejected(ValueError):
    pass


@dataclass(frozen=True)
class StoredDocument:
    filename: str
    sha256: str
    source_path: str
    size_bytes: int
    title: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class RegisteredDocument:
    document_id: int
    status: str
    inserted: bool
    queued: bool


def safe_filename(filename: str | None) -> str:
    name = re.split(r"[\\/]", filename or "")[-1].strip()
    return name or "document.pdf"


def store_pdf(
    source: BinaryIO,
    filename: str | None,
    upload_dir: Path,
    max_upload_bytes: int,
) -> StoredDocument:
    clean_name = safe_filename(filename)
    if not clean_name.casefold().endswith(".pdf"):
        raise UploadRejected("Only PDF files are supported")

    upload_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = upload_dir / f".upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    size_bytes = 0
    header = bytearray()

    try:
        with temporary_path.open("wb") as destination:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                size_bytes += len(block)
                if size_bytes > max_upload_bytes:
                    raise UploadRejected(
                        f"PDF exceeds the {max_upload_bytes} byte upload limit"
                    )
                if len(header) < 1024:
                    header.extend(block[: 1024 - len(header)])
                digest.update(block)
                destination.write(block)

        if size_bytes == 0:
            raise UploadRejected("The uploaded PDF is empty")
        if b"%PDF-" not in bytes(header):
            raise UploadRejected("The uploaded file does not have a PDF header")

        sha256 = digest.hexdigest()
        final_path = upload_dir / f"{sha256}.pdf"
        if final_path.exists():
            temporary_path.unlink(missing_ok=True)
        else:
            temporary_path.replace(final_path)

        return StoredDocument(
            filename=clean_name,
            sha256=sha256,
            source_path=str(final_path),
            size_bytes=size_bytes,
        )
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


TEXT_SOURCE_SUFFIX = ".litkg-text.json"
TEXT_SOURCE_SCHEMA = "litkg-text-document/1"
MAX_TEXT_BLOCKS = 1000


def is_text_source(path) -> bool:
    return str(path).endswith(TEXT_SOURCE_SUFFIX)


def _short_text(value, field, limit, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise UploadRejected(f"{field} must be a non-empty string of at most {limit} characters")
    return value


def canonical_text_document(document) -> dict:
    """Validate a QC-canonical text document and keep only the fields LitKG uses.

    The stored form is deterministic, so identical documents share a sha256.
    """
    if not isinstance(document, dict):
        raise UploadRejected("Each text document must be a JSON object")
    doc_id = _short_text(document.get("id"), "id", 200, required=True)
    external_id = _short_text(document.get("external_id"), "external_id", 200)
    title = _short_text(document.get("title"), "title", 2000)
    blocks = document.get("blocks")
    if not isinstance(blocks, list) or not 0 < len(blocks) <= MAX_TEXT_BLOCKS:
        raise UploadRejected(f"blocks must contain 1-{MAX_TEXT_BLOCKS} text blocks")
    clean, seen = [], set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str) or not block["text"].strip():
            raise UploadRejected(f"block {index} must have non-empty text")
        block_id = _short_text(str(block.get("id", f"block-{index + 1}")), "block id", 200, required=True)
        if block_id in seen:
            raise UploadRejected(f"duplicate block id {block_id!r}")
        seen.add(block_id)
        section = block.get("section")
        if section is not None and (not isinstance(section, str) or len(section) > 500):
            raise UploadRejected("block section must be a string of at most 500 characters")
        clean.append({"id": block_id, "section": section, "text": block["text"]})
    return {"schema": TEXT_SOURCE_SCHEMA, "id": doc_id, "external_id": external_id,
            "title": title, "blocks": clean}


def store_text_document(document, upload_dir: Path, max_document_bytes: int) -> StoredDocument:
    """Persist one text document as ``{sha256}.litkg-text.json`` (content addressed)."""
    canonical = canonical_text_document(document)
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > max_document_bytes:
        raise UploadRejected(f"Text document exceeds the {max_document_bytes} byte limit")
    sha256 = hashlib.sha256(raw).hexdigest()
    upload_dir.mkdir(parents=True, exist_ok=True)
    final_path = upload_dir / f"{sha256}{TEXT_SOURCE_SUFFIX}"
    if not final_path.exists():
        temporary_path = upload_dir / f".upload-{uuid.uuid4().hex}.tmp"
        try:
            temporary_path.write_bytes(raw)
            temporary_path.replace(final_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    label = canonical["external_id"] or canonical["id"]
    filename = safe_filename(re.sub(r"[^\w.:-]+", "_", label))[:180] + TEXT_SOURCE_SUFFIX
    return StoredDocument(filename=filename, sha256=sha256, source_path=str(final_path),
                          size_bytes=len(raw), title=canonical["title"],
                          external_id=canonical["external_id"] or canonical["id"])


def register_document(
    cur,
    *,
    stored: StoredDocument,
    force_reprocess: bool,
) -> RegisteredDocument:
    cur.execute(
        """
        INSERT INTO documents (sha256, filename, source_path, status, title, external_id)
        VALUES (%s, %s, %s, 'uploaded', %s, %s)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING id, status
        """,
        (stored.sha256, stored.filename, stored.source_path, stored.title, stored.external_id),
    )
    row = cur.fetchone()
    inserted = row is not None

    if row is None:
        cur.execute(
            """
            UPDATE documents
            SET
                filename = %s,
                source_path = %s,
                title = COALESCE(%s, title),
                external_id = COALESCE(%s, external_id),
                updated_at = now()
            WHERE sha256 = %s
            RETURNING id, status
            """,
            (stored.filename, stored.source_path, stored.title, stored.external_id, stored.sha256),
        )
        row = cur.fetchone()

    if not inserted and row['status'] in {'parsing','cataloging','extracting','chunked'}:
        if reconcile_stalled(cur, document_id=row['id']):
            cur.execute('SELECT id,status FROM documents WHERE id=%s', (row['id'],))
            row = cur.fetchone()
    # Never start a second parse while the same content is already active.
    # force_reprocess applies to a completed document; active work is reused.
    should_queue = (
        inserted
        or row["status"] in {"uploaded", "failed", "partial", "cancelled"}
        or (force_reprocess and row["status"] == "done")
    )
    if should_queue:
        cur.execute(
            """
            UPDATE documents
            SET status = 'queued', error = NULL, updated_at = now(),
                pipeline_generation = pipeline_generation + 1
            WHERE id = %s
            RETURNING status
            """,
            (row["id"],),
        )
        status = cur.fetchone()["status"]
    else:
        status = row["status"]

    return RegisteredDocument(
        document_id=row["id"],
        status=status,
        inserted=inserted,
        queued=should_queue,
    )
