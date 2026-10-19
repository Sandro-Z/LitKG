import logging
import os
import re
import requests
from lxml import etree
from pypdf import PdfReader

from app.doi import extract_document_doi, normalize_doi
from app.provenance import page_bounds, text_boundary
from app.page_mapping import PdfPageIndex


GROBID_URL = os.environ.get("GROBID_URL", "http://grobid:8070")
logger = logging.getLogger(__name__)


def call_grobid(pdf_path: str) -> str:
    url = f"{GROBID_URL}/api/processFulltextDocument"

    with open(pdf_path, "rb") as f:
        files = {"input": f}
        data = {
            "consolidateHeader": "1",
            "consolidateCitations": "0",
            "includeRawCitations": "0",
            "includeRawAffiliations": "0",
            "segmentSentences": "1",
            # requests encodes this list as repeated multipart form fields.
            "teiCoordinates": ["p", "s"],
        }
        resp = requests.post(url, files=files, data=data, timeout=180)

    resp.raise_for_status()
    return resp.text


def _text_content(nodes):
    texts = []
    for node in nodes:
        t = "".join(node.itertext())
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            texts.append(t)
    return "\n".join(texts).strip()


def _tei_doi_text(root):
    """Render text/link targets for DOI lookup without cited bibliographies."""
    parts = []

    def visit(node):
        if not isinstance(node.tag, str):
            return
        name = etree.QName(node).localname
        if name in {"listBibl", "biblStruct", "bibl"} or (
            name == "ref" and (node.get("type") or "").lower() == "bibr"
        ):
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                parts.append(child.tail)
        if name in {"ref", "ptr"}:
            target = node.get("target") or ""
            if re.match(r"https?://(?:dx\.)?doi\.org/", target, re.I):
                parts.append(f" {target} ")
        if name in {"p", "note"}:
            parts.append("\n\n")
        elif name in {"head", "div", "idno"}:
            parts.append("\n")

    visit(root)
    return "".join(parts)


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
        text, paragraph_spans = _paragraph_text_and_spans(paragraph)
        if not text:
            continue
        spans.extend({**span, "start": span["start"] + offset,
                      "end": span["end"] + offset} for span in paragraph_spans)
        texts.append(text)
        offset += len(text) + 1
    return {
        "section_title": title,
        "text": "\n".join(texts),
        "page_spans": spans,
    }


def _paragraph_text_and_spans(paragraph):
    """Keep inline text intact and let sentence coordinates refine paragraphs."""
    parts = []

    def visit(node, inherited):
        pages = _paragraph_pages(node)
        if pages == (None, None):
            pages = inherited
        if node.text:
            parts.append((node.text, pages))
        for child in node:
            # Adjacent <s> elements need a separator even without XML indentation.
            block = child.tag.rsplit('}',1)[-1] in {'s','p','cell','row','item','head','figDesc'}
            if block:
                parts.append((" ", pages))
            visit(child, pages)
            if block:
                parts.append((" ", pages))
            if child.tail:
                parts.append((child.tail, pages))

    visit(paragraph, (None, None))
    characters = []
    spans = []
    pending_space = False
    for text, (page_start, page_end) in parts:
        for character in text:
            if character.isspace():
                pending_space = True
                continue
            if pending_space and characters:
                characters.append(" ")
            pending_space = False
            start = len(characters)
            characters.append(character)
            if spans and (spans[-1]["page_start"], spans[-1]["page_end"]) == (page_start, page_end):
                spans[-1]["end"] = len(characters)
            else:
                spans.append({"start": start, "end": len(characters),
                              "page_start": page_start, "page_end": page_end})
    return "".join(characters), spans


def _parse_tei_xml(tei_xml: str):
    return etree.fromstring(
        tei_xml.encode("utf-8"),
        parser=etree.XMLParser(resolve_entities=False, no_network=True),
    )


def parse_tei_sections(tei_xml: str, *, include_text_doi: bool = True):
    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    root = _parse_tei_xml(tei_xml)

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
    if doi is None and include_text_doi:
        doi = extract_document_doi([_tei_doi_text(root)])
    metadata = {"title": title, "doi": doi}

    sections = []

    abstract_nodes = root.xpath("//tei:profileDesc/tei:abstract//tei:p", namespaces=ns)
    abstract = _section_from_paragraphs("Abstract", abstract_nodes)
    if abstract["text"]:
        sections.append(abstract)

    # Include direct body paragraphs, figure/table captions, lists, and appendix
    # prose. Selecting only div/p silently omitted these source locations.
    candidates = root.xpath("//tei:text/tei:body//*[self::tei:p or self::tei:figure or self::tei:table or self::tei:list or self::tei:note] | "
                            "//tei:text/tei:back//*[self::tei:p or self::tei:figure or self::tei:table or self::tei:list or self::tei:note][not(ancestor::tei:listBibl)]", namespaces=ns)
    candidate_set = set(candidates)
    groups = []
    for node in candidates:
        if any(parent in candidate_set for parent in node.iterancestors()):
            continue
        div = next((parent for parent in node.iterancestors() if parent.tag == '{http://www.tei-c.org/ns/1.0}div'), None)
        heads = div.xpath('./tei:head', namespaces=ns) if div is not None else []
        heading = _text_content(heads) if heads else 'Body'
        if not groups or groups[-1][0] != (heading, div):
            groups.append(((heading, div), []))
        groups[-1][1].append(node)
    for (heading, _), nodes in groups:
        section = _section_from_paragraphs(heading, nodes)
        if section['text']:
            sections.append(section)

    return metadata, sections


def fallback_pdf_text(pdf_path: str, *, doi_hint: str | None = None):
    reader = PdfReader(pdf_path)
    pdf_metadata = reader.metadata or {}
    title = str(pdf_metadata.get("/Title") or "").strip() or None
    doi = normalize_doi(pdf_metadata.get("/doi")) or normalize_doi(pdf_metadata.get("/DOI"))
    pages = []
    doi_texts = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        doi_texts.append(text)
        text = re.sub(r"(?<=\w)[-\u2010]\s*\n\s*(?=\w)", "", text)
        text = re.sub(r"\s+", " ", text).strip()
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
    if doi is None:
        doi = normalize_doi(doi_hint) or extract_document_doi(doi_texts)
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
            end = text_boundary(text, start + max_chars, start + max(1, max_chars // 2))
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

            start = max(start + 1, text_boundary(text, end - overlap_chars,
                max(start + 1, end - overlap_chars - 200, end - max_chars // 2)))

    return chunks


def parse_pdf_to_chunks(pdf_path: str):
    used_grobid = False
    try:
        tei = call_grobid(pdf_path)
        metadata, sections = parse_tei_sections(tei, include_text_doi=False)
        if not sections:
            raise RuntimeError("No text sections extracted from GROBID")
        used_grobid = True
    except Exception:
        metadata, sections = fallback_pdf_text(pdf_path)

    # A successful GROBID response can still omit coordinates (notably abstract
    # paragraphs), or only provide a whole paragraph's multi-page range.
    needs_pages = used_grobid and any(
        not section.get("page_spans") or any(
            span.get("page_start") is None or span.get("page_end") is None
            or span["page_start"] != span["page_end"]
            for span in section["page_spans"]
        ) for section in sections
    )
    tei_text_doi = None
    if used_grobid and not metadata.get("doi"):
        tei_text_doi = extract_document_doi([_tei_doi_text(_parse_tei_xml(tei))])

    if used_grobid and (needs_pages or not metadata.get("doi")):
        try:
            pdf_metadata, pdf_sections = fallback_pdf_text(pdf_path, doi_hint=tei_text_doi)
            metadata = {key: metadata.get(key) or pdf_metadata.get(key)
                        for key in ("title", "doi")}
            if needs_pages:
                index = PdfPageIndex.from_sections(pdf_sections)
                for section in sections:
                    section["page_spans"] = index.page_spans(
                        section["text"], section.get("page_spans") or [],
                    )
        except Exception as exc:
            # Keep useful GROBID text/metadata when a PDF text layer is absent.
            logger.warning("PDF metadata/page alignment unavailable (%s)", type(exc).__name__)

    if used_grobid and not metadata.get("doi"):
        # Structured TEI can retain an appendix/link omitted by PDF extraction,
        # including when the PDF text layer cannot be read at all.
        metadata["doi"] = tei_text_doi

    if not sections:
        raise RuntimeError("No text sections extracted from PDF")

    chunks = split_into_chunks(sections)
    return metadata, chunks
