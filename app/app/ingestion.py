import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class UploadRejected(ValueError):
    pass


@dataclass(frozen=True)
class StoredDocument:
    filename: str
    sha256: str
    source_path: str
    size_bytes: int


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


def register_document(
    cur,
    *,
    stored: StoredDocument,
    force_reprocess: bool,
) -> RegisteredDocument:
    cur.execute(
        """
        INSERT INTO documents (sha256, filename, source_path, status)
        VALUES (%s, %s, %s, 'uploaded')
        ON CONFLICT (sha256) DO NOTHING
        RETURNING id, status
        """,
        (stored.sha256, stored.filename, stored.source_path),
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
                updated_at = now()
            WHERE sha256 = %s
            RETURNING id, status
            """,
            (stored.filename, stored.source_path, stored.sha256),
        )
        row = cur.fetchone()

    # Never start a second parse while the same content is already active.
    # force_reprocess applies to a completed document; active work is reused.
    should_queue = (
        inserted
        or row["status"] in {"uploaded", "failed", "partial"}
        or (force_reprocess and row["status"] == "done")
    )
    if should_queue:
        cur.execute(
            """
            UPDATE documents
            SET status = 'queued', error = NULL, updated_at = now()
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
