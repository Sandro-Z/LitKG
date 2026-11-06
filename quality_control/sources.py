"""Attach independently read source documents to a normalized graph."""
import base64
import binascii
from collections import Counter
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

from .models import digest

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_LITKG_SOURCE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_TEXT = 2_000_000
MAX_UPLOADS = 64
SOURCE_ORIGINS = {"litkg_original", "uploaded_original"}


def _pdf_blocks(raw):
    if b"%PDF-" not in raw[:1024]:
        raise ValueError("SOURCE_INVALID_PDF")
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
                input=raw, stdout=output, stderr=subprocess.DEVNULL, timeout=30,
            )
            if process.returncode:
                raise ValueError("SOURCE_UNREADABLE")
            output.seek(0)
            data = output.read(MAX_SOURCE_TEXT * 4 + 1)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("SOURCE_UNREADABLE") from None
    if len(data) > MAX_SOURCE_TEXT * 4:
        raise ValueError("SOURCE_TEXT_TOO_LARGE")
    try:
        pages = data.decode("utf-8").split("\f")
    except UnicodeError:
        raise ValueError("SOURCE_UNREADABLE") from None
    return [
        dict(page=page, text=text.strip())
        for page, text in enumerate(pages, 1) if text.strip()
    ]


def _document(raw, filename, origin, complete=False):
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        blocks = _pdf_blocks(raw)
    elif suffix in {".txt", ".md"}:
        try:
            blocks = [dict(text=raw.decode("utf-8-sig"))]
        except UnicodeError:
            raise ValueError("SOURCE_INVALID_UTF8") from None
    else:
        raise ValueError("SOURCE_FILE_TYPE")
    if not any(b["text"].strip() for b in blocks):
        raise ValueError("SOURCE_NO_TEXT")
    if sum(len(b["text"]) for b in blocks) > MAX_SOURCE_TEXT:
        raise ValueError("SOURCE_TEXT_TOO_LARGE")
    checksum = hashlib.sha256(raw).hexdigest()
    for index, block in enumerate(blocks, 1):
        block.update(
            id=f"original:{checksum}:{block.get('page', index)}",
            kind="text", source_origin=origin, source_filename=filename,
        )
    return dict(
        blocks=blocks, source_sha256=checksum, original_source_status="attached",
        source_filename=filename, source_origin=origin,
        parsing_status="complete" if complete and suffix != ".pdf" else "partial",
    )


def _attach(doc, source):
    new_ids = {b["id"] for b in source["blocks"]}
    existing = [b for b in doc.get("blocks", []) if b["id"] not in new_ids]
    doc.update(source)
    doc["blocks"] = existing + source["blocks"]


def _refresh_hash(graph):
    graph["snapshot_hash"] = digest({k: v for k, v in graph.items() if k != "snapshot_hash"})


def attach_litkg_sources(graph, directory):
    """Locate the exact original PDF by the source hash recorded in LitKG."""
    root = Path(directory).resolve()
    counts = Counter()
    for doc in graph["documents"]:
        expected = doc.get("source_sha256")
        status = "missing"
        if isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected):
            file = root / f"{expected}.pdf"
            try:
                if not file.is_symlink() and file.is_file():
                    with file.open("rb") as handle:
                        raw = handle.read(MAX_LITKG_SOURCE_BYTES + 1)
                    if len(raw) > MAX_LITKG_SOURCE_BYTES:
                        status = "too_large"
                    elif hashlib.sha256(raw).hexdigest() != expected:
                        status = "hash_mismatch"
                    else:
                        _attach(doc, _document(raw, file.name, "litkg_original"))
                        status = "attached"
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                status = "unreadable"
        doc["original_source_status"] = status
        counts[status] += 1
    _refresh_hash(graph)
    return dict(counts)


def attach_uploaded_sources(graph, uploads, complete=False):
    if not isinstance(uploads, list) or len(uploads) > MAX_UPLOADS:
        raise ValueError("SOURCE_UPLOAD_INVALID")
    if not uploads:
        return {}
    docs = {d["id"]: d for d in graph["documents"]}
    referenced = {c["document_id"] for c in graph["claims"] if c.get("document_id")}
    for claim in graph["claims"]:
        for ref in claim.get("source_refs", []):
            doc_id = ref.get("doc_id", ref.get("document_id"))
            if doc_id is not None:
                referenced.add(str(doc_id))
    known_ids = referenced | docs.keys()
    total = 0
    attached = {}
    for item in uploads:
        if not isinstance(item, dict):
            raise ValueError("SOURCE_UPLOAD_INVALID")
        filename, encoded = item.get("filename"), item.get("content")
        if (not isinstance(filename, str) or not 0 < len(filename) <= 255
                or any(c in "/\\" or ord(c) < 32 for c in filename)
                or not isinstance(encoded, str)):
            raise ValueError("SOURCE_UPLOAD_INVALID")
        doc_id = item.get("document_id")
        if doc_id is None:
            stem = Path(filename).stem
            if not known_ids or stem in known_ids:
                doc_id = stem
            elif len(uploads) == 1 and len(known_ids) == 1:
                doc_id = next(iter(known_ids))
            else:
                raise ValueError("SOURCE_DOCUMENT_ID_UNMATCHED")
        if not isinstance(doc_id, str) or not doc_id.strip() or len(doc_id) > 200:
            raise ValueError("SOURCE_DOCUMENT_ID_INVALID")
        if doc_id in attached:
            raise ValueError("SOURCE_DOCUMENT_ID_DUPLICATE")
        if len(encoded) > 4 * ((MAX_SOURCE_BYTES - total + 2) // 3):
            raise ValueError("SOURCE_UPLOAD_TOO_LARGE")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("SOURCE_UPLOAD_INVALID") from None
        total += len(raw)
        if total > MAX_SOURCE_BYTES:
            raise ValueError("SOURCE_UPLOAD_TOO_LARGE")
        source = _document(raw, filename, "uploaded_original", complete is True)
        attached[doc_id] = (filename, source)
    for doc_id, (filename, source) in attached.items():
        if doc_id not in docs:
            docs[doc_id] = dict(id=doc_id, title=filename)
            graph["documents"].append(docs[doc_id])
        _attach(docs[doc_id], source)
    _refresh_hash(graph)
    return {"attached": len(attached)}


def review_blocks(corpus):
    """Use original pages when available while retaining old blocks for UI anchors."""
    original_docs = {
        key[0] for key, block in corpus.items() if block.get("source_origin") in SOURCE_ORIGINS
    }
    return {
        key: block for key, block in corpus.items()
        if key[0] not in original_docs or block.get("source_origin") in SOURCE_ORIGINS
    }
