import os
import re
import requests
from lxml import etree
from pypdf import PdfReader


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


def parse_tei_sections(tei_xml: str):
    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    root = etree.fromstring(tei_xml.encode("utf-8"))

    title_nodes = root.xpath("//tei:titleStmt/tei:title", namespaces=ns)
    title = _text_content(title_nodes) if title_nodes else None

    sections = []

    abstract_nodes = root.xpath("//tei:profileDesc/tei:abstract//tei:p", namespaces=ns)
    abstract_text = _text_content(abstract_nodes)
    if abstract_text:
        sections.append({
            "section_title": "Abstract",
            "text": abstract_text,
        })

    divs = root.xpath("//tei:text/tei:body//tei:div", namespaces=ns)
    for div in divs:
        head_nodes = div.xpath("./tei:head", namespaces=ns)
        p_nodes = div.xpath("./tei:p", namespaces=ns)

        heading = _text_content(head_nodes) if head_nodes else "Body"
        text = _text_content(p_nodes)

        if text and len(text) > 80:
            sections.append({
                "section_title": heading,
                "text": text,
            })

    return title, sections


def fallback_pdf_text(pdf_path: str):
    reader = PdfReader(pdf_path)
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            pages.append({
                "section_title": f"Page {i + 1}",
                "text": text,
            })
    return None, pages


def split_into_chunks(sections, max_chars=12000, overlap_chars=800):
    chunks = []

    for section in sections:
        title = section["section_title"]
        text = section["text"].strip()

        if len(text) <= max_chars:
            chunks.append({
                "section_title": title,
                "text": text,
                "token_estimate": max(1, len(text) // 3),
            })
            continue

        start = 0
        while start < len(text):
            end = start + max_chars
            chunk_text = text[start:end].strip()

            if chunk_text:
                chunks.append({
                    "section_title": title,
                    "text": chunk_text,
                    "token_estimate": max(1, len(chunk_text) // 3),
                })

            if end >= len(text):
                break

            start = max(0, end - overlap_chars)

    return chunks


def parse_pdf_to_chunks(pdf_path: str):
    try:
        tei = call_grobid(pdf_path)
        title, sections = parse_tei_sections(tei)
    except Exception:
        title, sections = fallback_pdf_text(pdf_path)

    if not sections:
        raise RuntimeError("No text sections extracted from PDF")

    chunks = split_into_chunks(sections)
    return title, chunks
