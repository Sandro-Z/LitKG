#!/usr/bin/env python3
"""Prepare BioRED / DiMB-RE gold standards as QC canonical documents + gold files.

Standard library only.  Official files are downloaded (or read from a local
raw directory), hashed, and converted into two system-neutral files:

* ``documents.<bench>-<split>.json`` – QC canonical documents.  Any system
  (LitKG, PubTator3, Neo4j LLM Graph Builder, ...) extracts from these texts
  and copies them verbatim into its exported graph.
* ``gold.<bench>-<split>.json`` – entities, relations, sentences, label sets
  and provenance used by ``quality_control.gold``.

Nothing is guessed: every label in ``labels`` is read from the data files and
every mention offset is asserted against the reconstructed text.

Usage::

    python3 benchmarks/prepare.py --bench biored --split test --download
    python3 benchmarks/prepare.py --bench dimbre --split test --download
    python3 benchmarks/prepare.py --all --download
"""
import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PREPARE_VERSION = 'litkg-benchmark-prepare/1.0.0'
GOLD_SCHEMA = 'litkg-gold/1'
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / 'data' / 'benchmarks' / 'raw'
DEFAULT_OUT = ROOT / 'data' / 'benchmarks'

BIORED_BASE = 'https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/'
BC8_BASE = 'https://ftp.ncbi.nlm.nih.gov/pub/lu/BC8-BioRED-track/'
DIMBRE_REPO = 'https://github.com/ScienceNLP-Lab/DiMB-RE'
DIMBRE_COMMIT = '03012c98dfcce0e66a7409469f8382799591d6e6'
DIMBRE_DIR = 'data/DiMB-RE/ner_reduced_v6.1_trg_abs_result'

SOURCES = {
    'biored': {
        'splits': {'train': 'BioRED/Train.PubTator', 'dev': 'BioRED/Dev.PubTator', 'test': 'BioRED/Test.PubTator'},
        'archive': 'BIORED.zip', 'url': BIORED_BASE + 'BIORED.zip', 'subdir': 'biored',
        'license': 'Public domain (US Government Work), see BioRED README.txt',
    },
    'bc8': {
        # Only the training file carries relations; val/test files have none.
        'splits': {'train': 'bc8_biored_task1_train.pubtator'},
        'archive': 'BC8_BioRED_Subtask1_PubTator.zip', 'url': BC8_BASE + 'BC8_BioRED_Subtask1_PubTator.zip',
        'subdir': 'bc8', 'license': 'Public domain (US Government Work), see BC8 README.txt',
    },
    'dimbre': {
        'splits': {s: f'{DIMBRE_DIR}/{s}.json' for s in ('train', 'dev', 'test')},
        'subdir': 'dimbre', 'commit': DIMBRE_COMMIT,
        'license': 'No license declared in the repository root; not redistributed',
    },
}


class PrepareError(RuntimeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def download(url, target):
    """Plain HTTPS GET (public datasets, no credentials)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'litkg-benchmark-prepare'})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    tmp = target.with_suffix(target.suffix + '.part')
    tmp.write_bytes(data)
    tmp.replace(target)
    return data


# ---------------------------------------------------------------- PubTator

def parse_pubtator(text):
    """Parse PubTator: ``PMID|t|``, ``PMID|a|``, mention rows (6 cols), relation rows (5 cols)."""
    docs, order = {}, []

    def doc(pmid):
        if pmid not in docs:
            docs[pmid] = dict(pmid=pmid, title=None, abstract=None, mentions=[], relations=[])
            order.append(pmid)
        return docs[pmid]

    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = re.match(r'^([^|\t]+)\|([ta])\|(.*)$', line)
        if match:
            field = 'title' if match.group(2) == 't' else 'abstract'
            doc(match.group(1))[field] = match.group(3)
            continue
        parts = line.split('\t')
        if len(parts) == 6 and parts[1].isdigit() and parts[2].isdigit():
            doc(parts[0])['mentions'].append(dict(start=int(parts[1]), end=int(parts[2]), text=parts[3],
                                                  type=parts[4], ids=[i.strip() for i in parts[5].split(',') if i.strip()]))
        elif len(parts) == 5:
            doc(parts[0])['relations'].append(dict(type=parts[1], id1=parts[2], id2=parts[3], novelty=parts[4]))
        else:
            raise PrepareError(f'PubTator line {number}: unrecognised row with {len(parts)} columns')
    return [docs[p] for p in order]


def convert_pubtator(raw_text, bench, split, source_meta):
    parsed = parse_pubtator(raw_text)
    if not any(d['relations'] for d in parsed):
        raise PrepareError(f'{bench}-{split}: file has no relation annotations; it cannot serve as a gold standard')
    documents, entities, relations = [], [], []
    checked = 0
    for d in parsed:
        if d['title'] is None or d['abstract'] is None:
            raise PrepareError(f'{bench}-{split} {d["pmid"]}: missing title or abstract')
        # PubTator offsets: abstract starts at len(title) + 1.
        text = d['title'] + ' ' + d['abstract']
        doc_id = f'biored:{d["pmid"]}'
        for m in d['mentions']:
            checked += 1
            if not (0 <= m['start'] < m['end'] <= len(text)) or text[m['start']:m['end']] != m['text']:
                raise PrepareError(f'{doc_id}: mention offset mismatch at {m["start"]}-{m["end"]} '
                                   f'({text[m["start"]:m["end"]]!r} != {m["text"]!r})')
            entities.append(dict(doc_id=doc_id, start=m['start'], end=m['end'], text=m['text'],
                                 type=m['type'], ids=m['ids']))
        for r in d['relations']:
            relations.append(dict(doc_id=doc_id, id1=r['id1'], id2=r['id2'], type=r['type'], novelty=r['novelty']))
        documents.append(dict(id=doc_id, external_id=doc_id, title=d['title'], parsing_status='complete',
                              blocks=[dict(id='ta', section='title+abstract', text=text)]))
    labels = dict(entity_types=sorted({e['type'] for e in entities}),
                  relation_types=sorted({r['type'] for r in relations}),
                  novelty=sorted({r['novelty'] for r in relations}))
    stats = dict(documents=len(documents), entities=len(entities), relations=len(relations),
                 mention_offsets_checked=checked, mention_offsets_valid=checked)
    gold = dict(schema=GOLD_SCHEMA, kind='biored', split=split, name=f'{bench}-{split}',
                documents=[dict(id=d['id'], external_id=d['external_id'], title=d['title'], text=d['blocks'][0]['text'])
                           for d in documents],
                entities=entities, relations=relations, sentences=[], labels=labels, meta=dict(source_meta, stats=stats))
    return documents, gold


# ------------------------------------------------------------------ DiMB-RE

def convert_dimbre(raw_text, split, source_meta):
    lines = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    documents, entities, relations, sentences = [], [], [], []
    checked = 0
    for d in lines:
        doc_id = f'dimbre:{d["doc_key"]}'
        tokens, spans, text_parts, pos = [], [], [], 0
        sentence_bounds = []
        for si, sent in enumerate(d['sentences']):
            first = len(tokens)
            for token in sent:
                if text_parts:
                    text_parts.append(' ')
                    pos += 1
                tokens.append(token)
                spans.append((pos, pos + len(token)))
                text_parts.append(token)
                pos += len(token)
            if len(tokens) == first:
                raise PrepareError(f'{doc_id}: empty sentence {si}')
            sentence_bounds.append((first, len(tokens) - 1))
            sentences.append(dict(doc_id=doc_id, index=si, start=spans[first][0], end=spans[-1][1],
                                  token_start=first, token_end=len(tokens) - 1))
        text = ''.join(text_parts)

        def char_span(start_token, end_token):
            # DiMB-RE spans are document-level, inclusive token indices.
            if not 0 <= start_token <= end_token < len(tokens):
                raise PrepareError(f'{doc_id}: token span {start_token}-{end_token} out of range')
            return spans[start_token][0], spans[end_token][1]

        def sentence_of(token):
            return next(i for i, (a, b) in enumerate(sentence_bounds) if a <= token <= b)

        for sent in d['ner']:
            for start_token, end_token, kind in sent:
                start, end = char_span(start_token, end_token)
                expected = ' '.join(tokens[start_token:end_token + 1])
                checked += 1
                if text[start:end] != expected:
                    raise PrepareError(f'{doc_id}: span {start_token}-{end_token} does not reproduce token text')
                entities.append(dict(doc_id=doc_id, start=start, end=end, text=expected, type=kind, ids=[],
                                     token_start=start_token, token_end=end_token, sentence_index=sentence_of(start_token)))
        for si, sent in enumerate(d['relations']):
            for rel in sent:
                if len(rel) != 6:
                    raise PrepareError(f'{doc_id}: relation without factuality: {rel!r}')
                hs, he, ts, te, kind, factuality = rel
                head, tail = char_span(hs, he), char_span(ts, te)
                relations.append(dict(doc_id=doc_id, sentence_index=si,
                                      sentence_indices=sorted({si, sentence_of(hs), sentence_of(ts)}),
                                      head_span=list(head), tail_span=list(tail),
                                      head_text=text[head[0]:head[1]], tail_text=text[tail[0]:tail[1]],
                                      type=kind, factuality=factuality))
        title = ' '.join(d['sentences'][0])
        documents.append(dict(id=doc_id, external_id=doc_id, title=title, parsing_status='complete',
                              text_reconstruction='tokens joined by single spaces; layout differs from the original article',
                              blocks=[dict(id='text', section='title+abstract(+results)', text=text)]))
    if not relations:
        raise PrepareError(f'dimbre-{split}: no relations')
    labels = dict(entity_types=sorted({e['type'] for e in entities}),
                  relation_types=sorted({r['type'] for r in relations}),
                  factuality=sorted({r['factuality'] for r in relations}))
    stats = dict(documents=len(documents), entities=len(entities), relations=len(relations),
                 sentences=len(sentences), token_spans_checked=checked, token_spans_valid=checked,
                 cross_sentence_relations=sum(1 for r in relations if len(r['sentence_indices']) > 1))
    gold = dict(schema=GOLD_SCHEMA, kind='dimbre', split=split, name=f'dimbre-{split}',
                documents=[dict(id=d['id'], external_id=d['external_id'], title=d['title'], text=d['blocks'][0]['text'])
                           for d in documents],
                entities=entities, relations=relations, sentences=sentences, labels=labels,
                meta=dict(source_meta, stats=stats))
    return documents, gold


# ------------------------------------------------------------------- driver

def load_raw(bench, split, raw_dir, allow_download):
    spec = SOURCES[bench]
    if split not in spec['splits']:
        raise PrepareError(f'{bench}: unsupported split {split!r} (available: {", ".join(spec["splits"])})')
    member = spec['splits'][split]
    folder = raw_dir / spec['subdir']
    files = []
    if bench == 'dimbre':
        target = folder / DIMBRE_COMMIT / f'{split}.json'
        url = f'https://raw.githubusercontent.com/ScienceNLP-Lab/DiMB-RE/{DIMBRE_COMMIT}/{member}'
        if target.exists():
            data = target.read_bytes()
        elif allow_download:
            data = download(url, target)
        else:
            raise PrepareError(f'missing {target}; rerun with --download')
        files.append(dict(name=member, url=url, sha256=sha256(data), bytes=len(data)))
        return data.decode('utf-8'), dict(source_url=url, repository=DIMBRE_REPO, commit=DIMBRE_COMMIT, files=files,
                                          license=spec['license'], split=split, member=member)
    archive = folder / spec['archive']
    if archive.exists():
        blob = archive.read_bytes()
    elif allow_download:
        blob = download(spec['url'], archive)
    else:
        raise PrepareError(f'missing {archive}; rerun with --download')
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        try:
            data = z.read(member)
        except KeyError:
            raise PrepareError(f'{archive.name} has no member {member}') from None
    files += [dict(name=spec['archive'], url=spec['url'], sha256=sha256(blob), bytes=len(blob)),
              dict(name=member, sha256=sha256(data), bytes=len(data), within=spec['archive'])]
    return data.decode('utf-8'), dict(source_url=spec['url'], files=files, license=spec['license'],
                                      split=split, member=member)


def prepare(bench, split, raw_dir=DEFAULT_RAW, out_dir=DEFAULT_OUT, allow_download=False, now=None):
    raw_text, meta = load_raw(bench, split, Path(raw_dir), allow_download)
    meta.update(prepared_at=(now or datetime.now(timezone.utc)).isoformat(timespec='seconds'),
                prepare_version=PREPARE_VERSION, benchmark=bench)
    if bench == 'dimbre':
        documents, gold = convert_dimbre(raw_text, split, meta)
    else:
        documents, gold = convert_pubtator(raw_text, bench, split, meta)
    name = f'{bench}-{split}'
    out = Path(out_dir)
    doc_path, gold_path = out / f'documents.{name}.json', out / f'gold.{name}.json'
    write_json(doc_path, documents)
    write_json(gold_path, gold)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    manifest.setdefault('note', 'Downloaded benchmark files are not redistributed; regenerate with benchmarks/prepare.py.')
    manifest.setdefault('outputs', {})[name] = dict(
        downloaded_or_read_at=meta['prepared_at'], source=meta,
        documents_file=doc_path.name, documents_sha256=sha256(doc_path.read_bytes()),
        gold_file=gold_path.name, gold_sha256=sha256(gold_path.read_bytes()), stats=gold['meta']['stats'])
    write_json(manifest_path, manifest)
    return gold['meta']['stats']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--bench', choices=sorted(SOURCES))
    parser.add_argument('--split')
    parser.add_argument('--all', action='store_true', help='biored train/dev/test and dimbre train/dev/test')
    parser.add_argument('--raw-dir', default=str(DEFAULT_RAW))
    parser.add_argument('--out-dir', default=str(DEFAULT_OUT))
    parser.add_argument('--download', action='store_true', help='download missing official files')
    args = parser.parse_args(argv)
    jobs = ([(b, s) for b in ('biored', 'dimbre') for s in ('train', 'dev', 'test')] if args.all
            else [(args.bench, args.split)])
    if not args.all and (not args.bench or not args.split):
        parser.error('--bench and --split are required unless --all is given')
    for bench, split in jobs:
        try:
            stats = prepare(bench, split, args.raw_dir, args.out_dir, args.download)
        except PrepareError as exc:
            print(f'ERROR {bench}-{split}: {exc}', file=sys.stderr)
            return 1
        print(f'{bench}-{split}: ' + ', '.join(f'{k}={v}' for k, v in stats.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
