"""kgb command line: prep, run, validate, stats."""
import argparse
import glob
import json
import platform
import re
import sys
import time
from datetime import date
from importlib import metadata as importlib_metadata
from pathlib import Path

from . import __version__, config as config_mod
from .common import digest, load_json, now, save_json, sha256_file, sha256_text
from .text_prep import build

ROOT = Path(__file__).resolve().parents[1]


def _inputs(cfg):
    paths = cfg['paths']
    documents, policy = load_json(paths['corpus']), load_json(paths['policy'])
    if isinstance(documents, dict) and 'documents' in documents:
        documents = documents['documents']
    if not isinstance(documents, list) or not isinstance(policy, dict):
        raise SystemExit('corpus/documents.json must be a list and corpus/policy.json an object')
    return documents, policy


def _version(package):
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return None


def _manifest_update(cfg, key, entry):
    out = Path(cfg['paths']['out'])
    path = out / 'run_manifest.json'
    manifest = load_json(path) if path.exists() else {}
    documents, policy = _inputs(cfg)
    id_map = Path(cfg['paths']['id_map'])
    manifest.update(kgb_version=__version__, python=platform.python_version(),
                    packages={p: _version(p) for p in ('httpx', 'neo4j')},
                    config=config_mod.public(cfg),
                    inputs=dict(corpus_sha256=digest(documents), policy_sha256=digest(policy),
                                id_map_sha256=sha256_file(id_map) if id_map.is_file() else None),
                    prompts={p.name: sha256_text(p.read_text(encoding='utf-8')) for p in sorted((ROOT / 'prompts').glob('*'))},
                    mappings={p.name: digest(load_json(p)) for p in sorted((ROOT / 'mappings').glob('*.json'))})
    manifest.setdefault('runs', {})[key] = entry
    save_json(path, manifest)


def _consistency(cfg, model, *, same_text=True, note_model=None):
    items = []
    litkg = cfg['llm'].get('litkg_model')
    if not litkg:
        items.append('LitKG extraction model not recorded (llm.litkg_model empty); model identity is unverified')
    elif model and model != litkg:
        items.append(f'extraction model {model!r} differs from LitKG model {litkg!r}')
    if note_model:
        items.append(note_model)
    if not same_text:
        items.append('input is the original PDF parsed by the external tool, not the LitKG chunk text')
    return items


def cmd_prep(cfg, args):
    documents, policy = _inputs(cfg)
    pdf_dir = Path(cfg['paths']['pdfs'])
    on_disk = {sha256_file(p): p.name for p in pdf_dir.glob('*.pdf')} if pdf_dir.is_dir() else {}
    report = dict(documents=len(documents), corpus_sha256=digest(documents), policy_sha256=digest(policy),
                  pdf_checked=0, pdf_matched=0, pdf_missing=[], texts=[])
    for doc in documents:
        expected = doc.get('source_sha256')
        if expected and doc.get('source_kind') != 'text':
            report['pdf_checked'] += 1
            if expected in on_disk:
                report['pdf_matched'] += 1
            else:
                report['pdf_missing'].append(str(doc['id']))
        dt = build(doc)
        from .pipelines.neo4j_builder import safe_filename
        text_path = Path(cfg['paths']['out']) / 'text' / f'{safe_filename(doc["id"])}.txt'
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(dt.text, encoding='utf-8')
        report['texts'].append(dict(doc_id=dt.doc_id, source=dt.source, chars=len(dt.text),
                                    segments=len(dt.segments), overlaps_removed=dt.overlaps_removed,
                                    text_sha256=sha256_text(dt.text)))
    save_json(Path(cfg['paths']['out']) / 'prep.json', report)
    print(f'{len(documents)} documents; PDFs checked {report["pdf_checked"]}, matched {report["pdf_matched"]}, '
          f'missing {len(report["pdf_missing"])}; overlaps removed in '
          f'{sum(1 for t in report["texts"] if t["overlaps_removed"])} documents')
    return 0


def cmd_run(cfg, args):
    documents, policy = _inputs(cfg)
    corpus = cfg['paths'].get('corpus_name') or Path(cfg['paths']['corpus']).stem
    started = time.monotonic()
    if args.system == 'pubtator3':
        from .pipelines import pubtator3 as p3
        id_map = p3.read_id_map(cfg['paths']['id_map'])
        if args.pmid_from_external_id:
            for doc in documents:
                m = re.fullmatch(r'(?:biored|pmid):(\d+)', str(doc.get('external_id') or doc['id']))
                if m and str(doc['id']) not in id_map:
                    id_map[str(doc['id'])] = dict(pmid=m.group(1), pmcid=None)
        client = p3.PubTatorClient(cfg['paths']['cache'], cfg['pubtator'])
        if args.variant == 'native':
            builder, meta = p3.run_native(documents, policy, cfg['pubtator'], id_map, client)
            model, consistency = None, ['PubTator3 relations come from PubTator (BioREx), not from the LitKG LLM']
        else:
            from .llm import LLM
            model = cfg['llm']['model']
            consistency = _consistency(cfg, model)
            if cfg['llm'].get('litkg_model') and model != cfg['llm']['litkg_model'] and not args.allow_model_mismatch:
                raise SystemExit('llm.model differs from llm.litkg_model; pass --allow-model-mismatch to record and continue')
            try:
                key = config_mod.secret(cfg['llm'], 'api_key_file', 'api_key_env')
            except config_mod.ConfigError:
                key = None  # local model without authentication
            llm = LLM(cfg['llm']['base_url'], model, key,
                      temperature=cfg['llm']['temperature'], max_tokens=cfg['llm']['max_tokens'],
                      timeout=cfg['llm']['timeout'])
            builder, meta = p3.run_llm(documents, policy, cfg['pubtator'], client, llm, cfg['paths']['cache'],
                                       concurrency=cfg['llm']['concurrency'])
        system, variant, out_name = ('PubTator3' if args.variant == 'native' else 'PubTator3+LLM',
                                     f'pubtator3-{args.variant}', f'graph.pubtator3-{args.variant}.json')
        version = 'PubTator3 API (no version endpoint); free-text RESTful + biocjson export'
        mapping_sha = meta.pop('relation_mapping_sha256')
    else:
        from .pipelines import neo4j_builder as nb
        password = config_mod.secret(cfg['neo4j'], 'password_file', 'password_env')
        client, store = nb.BuilderClient(cfg['neo4j'], password), nb.Neo4jStore(cfg['neo4j'], password)
        try:
            index_path = Path(cfg['paths']['corpus']).parent / 'pdf_index.json'
            pdf_index = load_json(index_path) if index_path.is_file() else None
            builder, meta = nb.run(documents, policy, cfg['neo4j'], args.variant, client, store, cfg['paths']['pdfs'],
                                   cache_root=cfg['paths']['cache'], pdf_index=pdf_index)
        except nb.BuilderError as exc:
            print(f'error: {exc}', file=sys.stderr, flush=True)
            return 2
        finally:
            store.close()
        model = cfg['neo4j'].get('model_name') or cfg['neo4j']['model']
        consistency = _consistency(cfg, cfg['neo4j'].get('model_name'), same_text=args.variant != 'pdfnative',
                                   note_model='temperature fixed to 0 by the builder (src/llm.py); max_tokens and '
                                              'response_format are not configurable through the builder API')
        if meta.get('schema_mode') == 'nodes+instructions':
            consistency.append(f'schema mode nodes+instructions: entity types enforced via allowedNodes; the '
                               f'{meta.get("schema_triples")} domain/range triples were too many to send (the builder '
                               f'repeats them in every request), so predicates and domain/range were given as '
                               f'additional_instructions and are not enforced (violations counted in stats)')
        if args.variant == 'no-schema':
            consistency.append('no schema guidance: labels/relationship types are free-form, mapped to Other/original')
        consistency.append('whole documents extracted: the builder container runs with MAX_TOKEN_CHUNK_SIZE='
                           f'{nb.BUILDER_ENV["MAX_TOKEN_CHUNK_SIZE"]} (upstream default 10000 keeps only the first '
                           '10,000 tokens of each document for non-neo4j.com users)')
        if meta.get('failed_documents'):
            consistency.append(f'INCOMPLETE: {len(meta["failed_documents"])} document(s) failed in the builder and '
                               'are missing from this graph (metadata.failed_documents)')
        system, variant = 'Neo4j LLM Graph Builder', f'neo4j-builder-{args.variant}'
        out_name = f'graph.neo4j-builder-{args.variant}.json'
        version = f'{cfg["neo4j"]["builder_ref"]} ({cfg["neo4j"]["builder_commit"]})'
        mapping_sha = meta.get('schema_sha256')
    elapsed = round(time.monotonic() - started, 1)
    metadata = dict(system_version=version, variant=variant, model=model,
                    prompt_sha256=meta.pop('prompt_sha256', None), mapping_sha256=mapping_sha,
                    elapsed_seconds=elapsed, consistency=consistency, **meta)
    graph = builder.result(f'{system} · {corpus} · {date.today().isoformat()}', metadata)
    out = Path(cfg['paths']['out']) / out_name
    save_json(out, graph)
    _manifest_update(cfg, variant, dict(output=out.name, finished_at=now(), elapsed_seconds=elapsed,
                                        system_version=version, model=model,
                                        prompt_sha256=metadata['prompt_sha256'], mapping_sha256=mapping_sha))
    s = graph['metadata']['stats']
    print(f'{out}: {s["nodes"]} nodes, {s["claims"]} claims, anchored {s["anchored"]}, unanchored {s["unanchored"]}')
    # Failed documents are listed next to the graph (not as graph.*.json): the scripts treat the
    # graph as incomplete while this file exists and retry those documents on the next run.
    failed_path = Path(cfg['paths']['out']) / f'failed.{variant}.json'
    failed = metadata.get('failed_documents') or []
    if failed:
        save_json(failed_path, failed)
        print(f'INCOMPLETE: {len(failed)} document(s) failed and are missing from the graph (listed in '
              f'{failed_path}); rerun to retry them, finished documents come from checkpoints', flush=True)
        return 3
    failed_path.unlink(missing_ok=True)
    return 0


def cmd_builder_schema(cfg, args):
    """Schema the Neo4j builder receives (JSON), for the in-container self-test of the scripts."""
    from .corpus import litkg_modules
    from .pipelines.neo4j_builder import schema
    policy, _ = litkg_modules(args.litkg_root)
    nodes, relationships, count, mode, instructions = schema(policy, 'schema', cfg['neo4j'].get('max_schema_triples', 1000))
    triples = relationships.split(',') if relationships else []
    print(json.dumps(dict(mode=mode, triples=count, nodes=nodes.split(',') if nodes else [],
                          relationships=[triples[i:i + 3] for i in range(0, len(triples), 3)],
                          instructions=instructions)))
    return 0


def cmd_validate(cfg, args):
    from .validate import validate
    documents, policy = _inputs(cfg)
    secrets = []
    for section, f, e in (('llm', 'api_key_file', 'api_key_env'), ('neo4j', 'password_file', 'password_env')):
        try:
            secrets.append(config_mod.secret(cfg[section], f, e))
        except config_mod.ConfigError:
            pass
    paths = [p for pattern in args.files for p in sorted(glob.glob(pattern)) if Path(p).name.startswith('graph.')]
    failed = 0
    for path in paths:
        errors, notes = validate(path, documents, policy, secrets, args.qc_path)
        print(f'{"FAIL" if errors else "PASS"} {path}')
        for line in errors:
            print(f'  error: {line}')
        for line in notes:
            print(f'  {line}')
        failed += bool(errors)
    if not paths:
        print('no graph.*.json files matched')
        return 1
    return 1 if failed else 0


def cmd_corpus(cfg, args):
    from .corpus import build as build_corpus
    stats = build_corpus(args.pdfs, Path(cfg['paths']['corpus']).parent, args.litkg_root, cfg['paths']['cache'],
                         resolve=not args.no_pmid)
    print(', '.join(f'{k}={v}' for k, v in stats.items() if not k.endswith('sha256')))
    return 0


def cmd_stats(cfg, args):
    from .stats import write
    print(write(cfg['paths']['out']))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kgb', description=__doc__)
    parser.add_argument('--config', default='config.toml')
    sub = parser.add_subparsers(dest='command', required=True)
    corpus = sub.add_parser('corpus', help='build corpus/documents.json, policy.json and id_map.csv from PDFs')
    corpus.add_argument('--pdfs', required=True)
    corpus.add_argument('--litkg-root', default=str(ROOT.parent))
    corpus.add_argument('--no-pmid', action='store_true', help='skip DOI -> PMID resolution (NCBI E-utilities)')
    sub.add_parser('prep')
    run = sub.add_parser('run')
    run.add_argument('system', choices=['pubtator3', 'neo4j'])
    run.add_argument('--variant', required=True)
    run.add_argument('--pmid-from-external-id', action='store_true',
                     help='derive PMIDs from external_id "biored:<pmid>" or "pmid:<pmid>" when id_map has none')
    run.add_argument('--allow-model-mismatch', action='store_true')
    val = sub.add_parser('validate')
    val.add_argument('files', nargs='+')
    val.add_argument('--qc-path', help='LitKG repository path for a read-only quality_control.models.normalize run')
    sub.add_parser('stats')
    bschema = sub.add_parser('builder-schema', help='print the schema sent to the Neo4j builder (JSON)')
    bschema.add_argument('--litkg-root', default=str(ROOT.parent))
    args = parser.parse_args(argv)
    if args.command == 'run':
        allowed = {'pubtator3': {'native', 'llm'}, 'neo4j': {'sametext', 'pdfnative', 'no-schema'}}[args.system]
        if args.variant not in allowed:
            parser.error(f'--variant must be one of {sorted(allowed)} for {args.system}')
    cfg = config_mod.load(args.config)
    return {'corpus': cmd_corpus, 'prep': cmd_prep, 'run': cmd_run, 'validate': cmd_validate, 'stats': cmd_stats,
            'builder-schema': cmd_builder_schema}[args.command](cfg, args)


if __name__ == '__main__':
    sys.exit(main())
