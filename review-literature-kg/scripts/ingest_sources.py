#!/usr/bin/env python3
"""Create anchored text blocks from local PDF/TXT/Markdown; never OCR silently."""
import argparse
import hashlib
import json
from pathlib import Path


def chunks(text, max_chars=12000):
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind('\n', start + max_chars // 2, end)
            if boundary > start:
                end = boundary + 1
        yield start, end, text[start:end]
        start = end


def ingest(manifest):
    documents, mappings, warnings = [], [], []
    seen = set()
    for item in manifest['documents']:
        if item['id'] in seen:
            raise ValueError('Duplicate document ID: ' + item['id'])
        seen.add(item['id'])
        path = Path(item['file']).resolve()
        raw = path.read_bytes()
        is_pdf = path.suffix.lower() == '.pdf'
        if is_pdf:
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError('PDF parsing needs pypdf; install it or provide extracted UTF-8 text.') from exc
            reader = PdfReader(path)
            pages = [(i+1, page.extract_text() or '') for i, page in enumerate(reader.pages)]
            status = 'partial'
            warnings.append({'doc_id':item['id'],'reason':'PDF text extracted; verify layout, tables, formulas, images and attachments before setting complete.'})
        elif path.suffix.lower() in ('.txt', '.md', '.markdown'):
            pages = [(None, raw.decode('utf-8-sig'))]
            status = 'complete'
        else:
            raise ValueError('Unsupported format; provide PDF or UTF-8 TXT/Markdown: '+path.suffix)
        blocks=[]
        for page, text in pages:
            if not text.strip():
                status='partial' if is_pdf else 'unreadable'
                warnings.append({'doc_id':item['id'],'page':page,'reason':'No readable text; not evidence of no facts. OCR/inspection required.'})
                continue
            for bi,(start,end,part) in enumerate(chunks(text),1):
                bid=f'p{page}-b{bi}' if page is not None else f'b{bi}'
                blocks.append({'id':bid,'page':page,'section':None,'kind':'text','text':part})
                mappings.append({'doc_id':item['id'],'block_id':bid,'page':page,
                                 'extracted_page_start':start,'extracted_page_end':end,
                                 'extracted_page_sha256':hashlib.sha256(text.encode()).hexdigest()})
        if not blocks: status='unreadable'
        documents.append({'id':item['id'],'title':item.get('title',path.name),
                          'version':item.get('version'),'source_file':str(path),
                          'source_sha256':hashlib.sha256(raw).hexdigest(),
                          'parsing_status':status,'blocks':blocks})
    return {'documents':documents,'mappings':mappings,'warnings':warnings}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('manifest')
    args=parser.parse_args()
    result=ingest(json.loads(Path(args.manifest).read_text(encoding='utf-8-sig')))
    print(json.dumps(result,ensure_ascii=False,indent=2))
