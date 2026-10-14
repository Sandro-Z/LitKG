import os
import re
import requests
from lxml import etree
from pypdf import PdfReader

from app.provenance import page_bounds


GROBID_URL = os.environ.get("GROBID_URL", "http://grobid:8070")


def call_grobid(pdf_path: str) -> str:
    url = f"{GROBID_URL}/api/processFulltextDocument"

    with open(pdf_path, "rb") as f:
        files = {"input": f}
        data = {
            "consolidateHeader": "1",
            "consolidateCitations": "0",
            "includeRawCitations": "0",
            "includeRawAffiliations": "0",
            "teiCoordinates": "p",
        }
        resp = requests.post(url, files=files, data=data, timeout=180)

    resp.raise_for_status()
    return resp.text


def _text_content(nodes):
    texts = []
    for node in nodes:
        t = " ".join(node.itertext())
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            texts.append(t)
    return "\n".join(texts).strip()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)",
        "",
        value.strip(),
        flags=re.I,
    )
    value = value.rstrip(".,;")
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value, flags=re.I) else None


def _paragraph_pages(node) -> tuple[int | None, int | None]:
    pages = []
    for coordinates in (node.get("coords") or "").split(";"):
        page = coordinates.split(",", 1)[0].strip()
        if page.isdigit() and int(page) > 0:
            pages.append(int(page))
    return (min(pages), max(pages)) if pages else (None, None)


def _section_from_paragraphs(title, paragraphs):
    texts = []
    spans = []
    offset = 0
    for paragraph in paragraphs:
        text = _text_content([paragraph])
        if not text:
            continue
        page_start, page_end = _paragraph_pages(paragraph)
        spans.append({
            "start": offset,
            "end": offset + len(text),
            "page_start": page_start,
            "page_end": page_end,
        })
        texts.append(text)
        offset += len(text) + 1
    return {
        "section_title": title,
        "text": "\n".join(texts),
        "page_spans": spans,
    }


def parse_tei_sections(tei_xml: str):
    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    root = etree.fromstring(
        tei_xml.encode("utf-8"),
        parser=etree.XMLParser(resolve_entities=False, no_network=True),
    )

    title_nodes = root.xpath("//tei:titleStmt/tei:title", namespaces=ns)
    title = _text_content(title_nodes) if title_nodes else None
    # Restrict DOI lookup to the paper header, excluding cited references.
    doi_nodes = root.xpath(
        "//tei:teiHeader/tei:fileDesc/tei:sourceDesc//tei:idno"
        "[translate(@type, 'doi', 'DOI') = 'DOI']",
        namespaces=ns,
    )
    doi = None
    for node in doi_nodes:
        doi = normalize_doi(_text_content([node]))
        if doi:
            break
    metadata = {"title": title, "doi": doi}

    sections = []

    abstract_nodes = root.xpath("//tei:profileDesc/tei:abstract//tei:p", namespaces=ns)
    abstract = _section_from_paragraphs("Abstract", abstract_nodes)
    if abstract["text"]:
        sections.append(abstract)

    divs = root.xpath("//tei:text/tei:body//tei:div", namespaces=ns)
    for div in divs:
        head_nodes = div.xpath("./tei:head", namespaces=ns)
        p_nodes = div.xpath("./tei:p", namespaces=ns)

        heading = _text_content(head_nodes) if head_nodes else "Body"
        section = _section_from_paragraphs(heading, p_nodes)
        if section["text"]:
            sections.append(section)

    return metadata, sections


def fallback_pdf_text(pdf_path: str):
    reader = PdfReader(pdf_path)
    pdf_metadata = reader.metadata or {}
    title = str(pdf_metadata.get("/Title") or "").strip() or None
    doi = normalize_doi(pdf_metadata.get("/doi") or pdf_metadata.get("/DOI"))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = re.sub(r"\s+", " ", text).strip()
        if i == 0 and doi is None:
            match = re.search(
                r"(?:https?://(?:dx\.)?doi\.org/|\bdoi\s*:\s*)"
                r"(10\.\d{4,9}/[^\s<>]+)",
                text,
                flags=re.I,
            )
            if match:
                doi = normalize_doi(match.group(1))
        if text:
            pages.append({
                "section_title": f"Page {i + 1}",
                "text": text,
                "page_spans": [{
                    "start": 0,
                    "end": len(text),
                    "page_start": i + 1,
                    "page_end": i + 1,
                }],
            })
    return {"title": title, "doi": doi}, pages


def split_into_chunks(sections, max_chars=12000, overlap_chars=800):
    if max_chars <= 0 or not 0 <= overlap_chars < max_chars:
        raise ValueError("Require max_chars > 0 and 0 <= overlap_chars < max_chars")
    chunks = []

    for section in sections:
        title = section["section_title"]
        text = section["text"]

        start = 0
        while start < len(text):
            end = start + max_chars
            raw_chunk_text = text[start:end]
            chunk_text = raw_chunk_text.strip()

            if chunk_text:
                content_start = start + len(raw_chunk_text) - len(raw_chunk_text.lstrip())
                content_end = content_start + len(chunk_text)
                spans = [
                    {
                        **span,
                        "start": max(span["start"], content_start) - content_start,
                        "end": min(span["end"], content_end) - content_start,
                    }
                    for span in section.get("page_spans", [])
                    if span["start"] < content_end and span["end"] > content_start
                ]
                page_start, page_end = page_bounds(spans)
                chunks.append({
                    "section_title": title,
                    "text": chunk_text,
                    "token_estimate": max(1, len(chunk_text) // 3),
                    "page_start": page_start,
                    "page_end": page_end,
                    "page_spans": spans,
                })

            if end >= len(text):
                break

            start = max(0, end - overlap_chars)

    return chunks


def parse_pdf_to_chunks(pdf_path: str):
    try:
        tei = call_grobid(pdf_path)
        metadata, sections = parse_tei_sections(tei)
        if not sections:
            raise RuntimeError("No text sections extracted from GROBID")
    except Exception:
        metadata, sections = fallback_pdf_text(pdf_path)

    if not sections:
        raise RuntimeError("No text sections extracted from PDF")

    chunks = split_into_chunks(sections)
    return metadata, chunks
