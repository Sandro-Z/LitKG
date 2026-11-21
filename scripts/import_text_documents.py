#!/usr/bin/env python3
"""Import QC-canonical text documents into a LitKG knowledge base.

Example::

    python3 scripts/import_text_documents.py --kb 7 \
        --file data/benchmarks/documents.biored-test.json

Documents are posted in batches to
``POST /knowledge-bases/{kb}/text-documents``; LitKG deduplicates them by
content hash and runs the normal parse -> catalog -> extraction pipeline.
Standard library only; no credentials are sent or printed.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def post(api, kb, documents, batch_name, force):
    query = urllib.parse.urlencode({k: v for k, v in (('batch_name', batch_name),
                                                       ('force_reprocess', 'true' if force else None)) if v})
    url = f'{api.rstrip("/")}/knowledge-bases/{kb}/text-documents' + (f'?{query}' if query else '')
    body = json.dumps(documents, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--kb', type=int, required=True, help='knowledge base id')
    parser.add_argument('--file', required=True, help='documents JSON array (QC canonical)')
    parser.add_argument('--api', default='http://127.0.0.1:8080', help='LitKG API base URL')
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--batch-name', default=None)
    parser.add_argument('--force-reprocess', action='store_true')
    args = parser.parse_args(argv)
    with open(args.file, encoding='utf-8') as handle:
        documents = json.load(handle)
    if isinstance(documents, dict):
        documents = documents.get('documents', [])
    if not isinstance(documents, list) or not documents:
        parser.error('--file must contain a non-empty JSON array of documents')
    size = max(1, args.batch_size)
    totals = {}
    for start in range(0, len(documents), size):
        chunk = documents[start:start + size]
        name = args.batch_name or f'{args.file.rsplit("/", 1)[-1]} [{start + 1}-{start + len(chunk)}]'
        try:
            batch = post(args.api, args.kb, chunk, name, args.force_reprocess)
        except urllib.error.HTTPError as exc:
            print(f'ERROR batch starting at {start}: HTTP {exc.code}', file=sys.stderr)
            return 1
        except urllib.error.URLError as exc:
            print(f'ERROR: LitKG API unreachable ({type(exc.reason).__name__})', file=sys.stderr)
            return 1
        for item in batch.get('items', []):
            totals[item.get('status')] = totals.get(item.get('status'), 0) + 1
        print(f'batch {batch.get("id")}: {len(chunk)} documents, status={batch.get("status")}')
    print('item statuses: ' + ', '.join(f'{k}={v}' for k, v in sorted(totals.items(), key=str)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
