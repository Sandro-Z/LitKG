"""Build the shared corpus directly from a folder of PDFs (no LitKG/QC instance needed).

* documents.json: one QC canonical document per PDF; id = PDF sha256 (the same value
  LitKG stores as ``source_sha256``), one block per page. Text is extracted with pypdf
  using the same clean-up as LitKG's ``fallback_pdf_text`` (de-hyphenation and
  whitespace collapsing).
* policy.json: LitKG's own ontology (entity types and predicate registry), identical
  to the ``policy`` of a LitKG snapshot imported into QC.
* id_map.csv: doc_id,pmid,pmcid, resolved from the DOI printed in the PDF
  (LitKG's DOI extractor) via NCBI E-utilities; used for PubTator precomputed relations.
* pdf_index.json: sha256 -> PDF path (local only, not part of any output graph).
"""
import csv
import re
import sys
from pathlib import Path

from .cache import Cache
from .common import digest, save_json, sha256_file, sha256_text
from .http import Http, HttpError

ESEARCH = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi'


def litkg_modules(repo_root):
    sys.path.insert(0, str(Path(repo_root) / 'app'))
    from app.doi import extract_document_doi  # noqa: E402  (pure Python)
    from app.entity_types import TYPE_DEFINITIONS, TYPE_PARENTS
    from app.predicate_registry import PREDICATES
    policy = dict(scope=None, ontology=dict(node_types=TYPE_DEFINITIONS, predicates=PREDICATES, type_parents=TYPE_PARENTS))
    return policy, extract_document_doi


def pdf_pages(path):
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    meta = reader.metadata or {}
    title = str(meta.get('/Title') or '').strip() or None
    pages, raw = [], []
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ''
        raw.append(text)
        text = re.sub(r'(?<=\w)[-‐]\s*\n\s*(?=\w)', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            pages.append((number, text))
    return title, pages, raw


def resolve_pmid(http, cache, doi):
    key = sha256_text(f'doi:{doi.casefold()}')
    hit = cache.get(key)
    if hit is not None:
        return hit.get('pmid')
    try:
        response = http.request('GET', ESEARCH, params={'db': 'pubmed', 'term': f'{doi}[doi]', 'retmode': 'json',
                                                        'tool': 'litkg-baselines'})
        ids = response.json().get('esearchresult', {}).get('idlist', [])
    except (HttpError, ValueError):
        return None
    pmid = ids[0] if len(ids) == 1 else None   # ambiguous or missing: no guess
    cache.put(key, {'pmid': pmid, 'hits': len(ids)})
    return pmid


def build(pdf_dir, out_dir, repo_root, cache_root, resolve=True, log=print):
    pdf_dir, out_dir = Path(pdf_dir), Path(out_dir)
    files = sorted(p for p in pdf_dir.rglob('*') if p.is_file() and p.suffix.casefold() == '.pdf')
    if not files:
        raise SystemExit(f'no PDF files under {pdf_dir}')
    policy, extract_doi = litkg_modules(repo_root)
    http, cache = Http(per_second=3), Cache(cache_root, 'pubmed-esearch')
    documents, index, id_rows, seen = [], {}, [], set()
    stats = dict(pdfs=len(files), duplicates=0, unreadable=0, no_text=0, doi=0, pmid=0)
    for path in files:
        sha = sha256_file(path)
        if sha in seen:
            stats['duplicates'] += 1
            continue
        seen.add(sha)
        try:
            title, pages, raw = pdf_pages(path)
        except Exception as exc:  # corrupt or encrypted PDF: reported, not fatal
            stats['unreadable'] += 1
            log(f'unreadable PDF skipped: {path.name} ({type(exc).__name__})')
            continue
        if not pages:
            stats['no_text'] += 1
            log(f'PDF without a text layer skipped (needs OCR): {path.name}')
            continue
        index[sha] = str(path.resolve())
        documents.append(dict(id=sha, title=title or path.stem, source_sha256=sha, source_filename=path.name,
                              parsing_status='partial', text_extraction='pypdf (LitKG fallback_pdf_text clean-up)',
                              blocks=[dict(id=f'p{n}', page=n, page_end=n, section=f'Page {n}', text=t)
                                      for n, t in pages]))
        doi = extract_doi(raw)
        pmid = resolve_pmid(http, cache, doi) if (resolve and doi) else None
        stats['doi'] += bool(doi)
        stats['pmid'] += bool(pmid)
        id_rows.append(dict(doc_id=sha, pmid=pmid or '', pmcid='', doi=doi or ''))
    out_dir.mkdir(parents=True, exist_ok=True)
    save_json(out_dir / 'documents.json', documents)
    save_json(out_dir / 'policy.json', policy)
    save_json(out_dir / 'pdf_index.json', index)
    with open(out_dir / 'id_map.csv', 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['doc_id', 'pmid', 'pmcid', 'doi'])
        writer.writeheader()
        writer.writerows(id_rows)
    stats.update(documents=len(documents), corpus_sha256=digest(documents), policy_sha256=digest(policy))
    save_json(out_dir / 'corpus_stats.json', stats)
    return stats
