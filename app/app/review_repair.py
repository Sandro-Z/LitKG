"""Scoped, preview-first repair. Never re-extract an entire knowledge base.

python -m app.review_repair --document-id 31 --mode repair --output plan.json
python -m app.review_repair --apply-plan plan.json
python -m app.review_repair --rollback RUN_UUID
"""
import argparse
import json
import os
import uuid
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from app.catalog import digest, load_catalog, archive_claims, add_review
from app.claims import prepare_claims
from app.input_quality import inspect_document, correct_chunk
from app.normalization import stable_json
from app.schemas import Claim, ClaimBundle
from app.provenance import locate_evidence_pages


ACTIVE = {'queued', 'parsing', 'chunked', 'cataloging', 'extracting'}


def json_value(value):
    return json.loads(stable_json(value))


def source_state(document, chunks, claims):
    return json_value({'document': {k: document.get(k) for k in ('id', 'sha256', 'catalog_version', 'catalog_status', 'pipeline_generation')},
        'chunks': chunks, 'claims': claims})


def make_plan(document, chunks, claims, entries, *, mode='repair', corrections=(), extractor=None, claim_scope=False):
    """Pure planning seam: tests supply fixed model responses and source text."""
    if document['status'] in ACTIVE or any(c['status'] in ACTIVE for c in chunks):
        raise ValueError('Selected document/chunk is active; retry after processing completes')
    if corrections and (mode != 'reextract' or claim_scope):
        raise ValueError('Source corrections require document-scoped reextract; every affected assertion must be reconsidered')
    chunk_map = {c['id']: c for c in chunks}
    if any(e.get('chunk_id') not in chunk_map for e in corrections):
        raise ValueError('Source correction is outside selected document/chunks')
    edits_by_chunk = {cid: [e for e in corrections if e['chunk_id'] == cid] for cid in chunk_map}
    corrected = {cid: correct_chunk(c, edits_by_chunk[cid]) for cid,c in chunk_map.items()}
    plan = {'schema_version': 1, 'run_id': str(uuid.uuid4()), 'mode': mode, 'dry_run': True,
        'document_id': document['id'], 'claim_scope': claim_scope,
        'before': source_state(document, chunks, claims), 'corrections': list(corrections),
        'changes': [], 'reviews': [], 'input_issues': inspect_document(chunks),
        'limitations': ['Reproject only rebuilds saved assertions; missing facts require reextract.',
                        'Reextract uses the current document catalog; out-of-catalog mentions remain independent and reviewable.']}
    plan['before_hash'] = digest(plan['before'])

    def review(chunk_id, reason, **detail):
        plan['reviews'].append({'chunk_id': chunk_id, 'reason': reason, **detail})

    def checked(bundle, chunk):
        result = prepare_claims(bundle, entries, chunk, document.get('catalog_version'))
        if result._claim_rejections:
            for rejected in result._claim_rejections:
                review(chunk['id'], rejected['error'], candidate=rejected.get('claim'))
            return None
        for claim in result.claims:
            for evidence in [claim.evidence, *claim.additional_evidence]:
                evidence.page, evidence.page_end = locate_evidence_pages(evidence.sentence, chunk)
                evidence.section = chunk.get('section_title')
        return [c.model_dump(mode='json') for c in result.claims]

    if mode == 'reextract':
        if extractor is None:
            raise ValueError('Reextract requires an explicit extractor')
        units = [(chunk, [c for c in claims if c['chunk_id'] == chunk['id']]) for chunk in chunks]
        if claim_scope:
            units = [(chunk_map[c['chunk_id']], [c]) for c in claims]
        for original_chunk, selected in units:
            chunk = corrected[original_chunk['id']]
            if not chunk['text'].strip():
                values, basis = [], 'Explicitly excluded article region, with recorded correction basis'
            else:
                kwargs = {}
                if claim_scope:
                    kwargs = {'feedback': 'Re-evaluate only this candidate against the source. Correct semantic roles, causality and required conditions. Do not preserve an unsupported original relationship. Explicitly exclude only with source justification.',
                              'previous_output': {'claims': [{**selected[0]['claim_json'], 'repair_id': '0'}]}}
                bundle = extractor(chunk.get('section_title') or '', chunk['text'], entries, **kwargs)
                if bundle._resolution and bundle._resolution['outcome'] == 'unresolved':
                    review(chunk['id'], bundle._resolution['reason'], claim_id=selected[0]['id'] if selected else None)
                    continue
                values = checked(bundle, chunk)
                basis = bundle._resolution['reason'] if bundle._resolution else 'Re-extracted from the scoped source with current semantic validation'
            if values is None:
                continue  # Never replace valid old records after a failed extraction.
            plan['changes'].append({'operation': 'replace', 'chunk_id': chunk['id'],
                'delete_ids': [c['id'] for c in selected], 'claims': values, 'basis': basis})
    else:
        for original in claims:
            if mode == 'reproject':
                plan['changes'].append({'operation': 'reproject', 'claim_id': original['id'],
                    'basis': 'Rebuild only existing facts/participants/projection; no new facts'})
                continue
            try:
                values = checked(ClaimBundle(claims=[Claim.model_validate(original['claim_json'])]), chunk_map[original['chunk_id']])
            except ValueError as exc:
                review(original['chunk_id'], str(exc), claim_id=original['id'])
                values = None
            if values is None and isinstance(original['claim_json'], dict):
                # Preserve a structurally invalid legacy assertion verbatim,
                # while preventing it from remaining a confirmed graph edge.
                reasons = [r['reason'] for r in plan['reviews'] if r['chunk_id'] == original['chunk_id']]
                after = {**original['claim_json'], 'semantic_status':'pending',
                    'review_reasons': list(dict.fromkeys([*original['claim_json'].get('review_reasons', []), *reasons]))}
                plan['changes'].append({'operation':'quarantine', 'claim_id':original['id'], 'after':after,
                    'basis':'Structural validation failed; preserve original roles and evidence for review'})
                continue
            if values is not None and values[0] != original['claim_json']:
                plan['changes'].append({'operation': 'replace', 'chunk_id': original['chunk_id'],
                    'delete_ids': [original['id']], 'claims': values,
                    'basis': 'Grounded result normalization and semantic review; no invented facts'})
    plan['diff'] = describe_diff(plan)
    # A corrected source must not be committed while any affected extraction is unresolved.
    plan['applicable'] = not (corrections and plan['reviews'])
    return plan


def describe_diff(plan):
    before = {c['id']: c for c in plan['before']['claims']}
    added, deleted, modified = [], [], []
    for change in plan['changes']:
        if change['operation'] == 'reproject':
            modified.append({'id': change['claim_id'], 'projection_only': True, 'basis': change['basis']})
        elif change['operation'] == 'quarantine':
            modified.append({'id':change['claim_id'], 'before':before[change['claim_id']]['claim_json'],
                             'after':change['after'], 'basis':change['basis']})
        else:
            old = [before[i] for i in change['delete_ids']]
            new = change['claims']
            # Pairing is for a human-readable diff, not production ID heuristics.
            for i in range(max(len(old), len(new))):
                if i < len(old) and i < len(new):
                    modified.append({'id': old[i]['id'], 'before': old[i]['claim_json'], 'after': new[i], 'basis': change['basis']})
                elif i < len(old):
                    deleted.append({'id': old[i]['id'], 'before': old[i]['claim_json'], 'basis': change['basis']})
                else:
                    added.append({'after': new[i], 'basis': change['basis']})
    return {'added': added, 'deleted': deleted, 'modified': modified}


def load_scope(cur, *, document_id=None, claim_ids=None):
    if (document_id is None) == (not claim_ids):
        raise ValueError('Select exactly one document or an explicit set of claims')
    if claim_ids:
        cur.execute('SELECT * FROM claims WHERE id=ANY(%s::BIGINT[]) ORDER BY id', (claim_ids,))
        claims = cur.fetchall()
        if {c['id'] for c in claims} != set(claim_ids):
            raise ValueError('Some selected claims no longer exist')
        docs = {c['document_id'] for c in claims}
        if len(docs) != 1:
            raise ValueError('Select claims from one document per repair plan')
        document_id = docs.pop()
    else:
        cur.execute('SELECT * FROM claims WHERE document_id=%s ORDER BY id', (document_id,))
        claims = cur.fetchall()
    cur.execute('SELECT * FROM documents WHERE id=%s', (document_id,))
    document = cur.fetchone()
    if not document:
        raise ValueError('Document not found')
    cur.execute('SELECT * FROM chunks WHERE document_id=%s ORDER BY id', (document_id,))
    chunks = cur.fetchall()
    if claim_ids:
        chunks = [c for c in chunks if c['id'] in {x['chunk_id'] for x in claims}]
    return document, chunks, claims


def _projection_backup(cur, claim_ids):
    backups = {}
    for table, where in (
        ('relation_evidence', 'claim_id=ANY(%s::BIGINT[])'),
        ('facts', 'claim_id=ANY(%s::BIGINT[])'),
        ('fact_participants', 'fact_id IN (SELECT id FROM facts WHERE claim_id=ANY(%s::BIGINT[]))'),
        ('fact_evidence', 'fact_id IN (SELECT id FROM facts WHERE claim_id=ANY(%s::BIGINT[]))')):
        cur.execute(f'SELECT * FROM {table} WHERE {where}', (claim_ids,))
        backups[table] = json_value(cur.fetchall())
    return backups


def _insert_rows(cur, table, rows):
    for row in rows:
        query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),
            sql.SQL(',').join(map(sql.Identifier, row)), sql.SQL(',').join(sql.Placeholder() for _ in row))
        cur.execute(query, tuple(Jsonb(v) if isinstance(v, (dict, list)) else v for v in row.values()))


def apply_plan(plan):
    """One guarded transaction; a saved plan never invokes the model on apply."""
    from app.db import get_conn
    from app.graph_store import lock_graph_projection, store_claim, project_existing_claim
    if plan.get('schema_version') != 1 or not plan.get('applicable'):
        raise ValueError('Plan is invalid or has unresolved source corrections')
    with get_conn() as conn:
        with conn.cursor() as cur:
            lock_graph_projection(cur, True)
            doc_id = plan['document_id']
            cur.execute('SELECT id FROM documents WHERE id=%s FOR UPDATE NOWAIT', (doc_id,))
            cur.execute('SELECT id FROM chunks WHERE document_id=%s FOR UPDATE NOWAIT', (doc_id,))
            document, chunks, claims = load_scope(cur, document_id=None if plan['claim_scope'] else doc_id,
                claim_ids=[c['id'] for c in plan['before']['claims']] if plan['claim_scope'] else None)
            if document['status'] in ACTIVE or any(c['status'] in ACTIVE for c in chunks):
                raise ValueError('Document is active')
            if digest(source_state(document, chunks, claims)) != plan['before_hash']:
                raise ValueError('Source or claims changed since preview; create a new plan')
            entries = load_catalog(cur, doc_id, document.get('catalog_version'))
            chunk_map = {c['id']: c for c in chunks}
            before_projection = _projection_backup(cur, [c['id'] for c in claims])
            cur.execute('SELECT id FROM claims WHERE chunk_id=ANY(%s::BIGINT[])', (list(chunk_map),))
            existing_ids = {c['id'] for c in cur.fetchall()}
            cur.execute('SELECT * FROM review_items WHERE document_id=%s ORDER BY id', (doc_id,))
            before_reviews = cur.fetchall()
            for cid in chunk_map:
                archive_claims(cur, doc_id, cid)
            for cid, chunk in chunk_map.items():
                edits = [e for e in plan['corrections'] if e['chunk_id'] == cid]
                if edits:
                    updated = correct_chunk(chunk, edits)
                    cur.execute('UPDATE chunks SET text=%s,page_spans=%s,updated_at=now() WHERE id=%s',
                        (updated['text'], Jsonb(updated['page_spans']), cid))
                    chunk_map[cid] = updated
            written = []
            allowed_ids = {c['id'] for c in claims}
            for change in plan['changes']:
                if change['operation'] == 'quarantine':
                    if change['claim_id'] not in allowed_ids:
                        raise ValueError('Quarantine outside selected claims')
                    original = next(c for c in claims if c['id'] == change['claim_id'])
                    after = change['after']
                    if {k:v for k,v in after.items() if k not in {'semantic_status','review_reasons'}} != {k:v for k,v in original['claim_json'].items() if k not in {'semantic_status','review_reasons'}} or after.get('semantic_status') != 'pending':
                        raise ValueError('Quarantine must preserve original assertion and evidence')
                    cur.execute('UPDATE claims SET claim_json=%s WHERE id=%s', (Jsonb(after),original['id']))
                    continue
                if change['operation'] == 'reproject':
                    if change['claim_id'] not in allowed_ids:
                        raise ValueError('Projection operation outside selected claims')
                    original = next(c for c in claims if c['id'] == change['claim_id'])
                    project_existing_claim(cur, original['id'], original['claim_json'], document.get('abbreviation_map'), doc_id,
                                           reconcile_existing=False)
                    continue
                if not set(change['delete_ids']) <= allowed_ids or change['chunk_id'] not in chunk_map:
                    raise ValueError('Replacement outside selected scope')
                bundle = prepare_claims(ClaimBundle(claims=change['claims']), entries, chunk_map[change['chunk_id']], document.get('catalog_version'))
                if bundle._claim_rejections:
                    raise ValueError('Planned replacement no longer validates')
                cur.execute('DELETE FROM claims WHERE id=ANY(%s::BIGINT[])', (change['delete_ids'],))
                for claim in bundle.claims:
                    new_id, _ = store_claim(cur, document_id=doc_id, chunk_id=change['chunk_id'], claim=claim,
                        extraction_model='review-repair' if plan['mode'] != 'reextract' else plan.get('model', 'unknown'),
                        abbreviations=document.get('abbreviation_map'))
                    if new_id in existing_ids - set(change['delete_ids']):
                        raise ValueError('Replacement conflicts with an unselected existing assertion')
                    written.append(new_id)
            for issue in plan['input_issues']:
                matching = [edit for edit in plan['corrections'] if edit['chunk_id'] == issue['chunk_id']
                            and edit['start'] <= issue['start'] and edit['end'] >= issue['end']]
                add_review(cur, doc_id, document.get('catalog_version'), 'input_quality',
                    {**issue, 'run_id': plan['run_id'], 'corrections': matching}, status='resolved' if matching else 'pending')
            for issue in plan['reviews']:
                add_review(cur, doc_id, document.get('catalog_version'), 'claim_rejection', {**issue, 'run_id': plan['run_id']})
            cur.execute('INSERT INTO derived_dirty_documents(document_id) VALUES (%s) ON CONFLICT(document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1', (doc_id,))
            # Record all claims in affected chunks to detect concurrent additions
            # before rollback, even for a claim-scoped repair.
            cur.execute('SELECT * FROM claims WHERE chunk_id=ANY(%s::BIGINT[]) ORDER BY id', (list(chunk_map),))
            after_claims = cur.fetchall()
            cur.execute('SELECT * FROM chunks WHERE id=ANY(%s::BIGINT[]) ORDER BY id', (list(chunk_map),))
            after_chunks = cur.fetchall()
            cur.execute('SELECT * FROM review_items WHERE document_id=%s ORDER BY id', (doc_id,))
            after_reviews = cur.fetchall()
            record = {'plan': plan, 'projection': before_projection, 'written_ids': written,
                      'before_reviews': before_reviews, 'after_reviews_hash': digest(after_reviews),
                      'after_claims_hash': digest(after_claims), 'after_chunks_hash': digest(after_chunks),
                      'after_document': source_state(document, [], [])['document']}
            cur.execute('INSERT INTO scoped_repair_runs(id,document_id,record) VALUES (%s::UUID,%s,%s)',
                (plan['run_id'], doc_id, Jsonb(json_value(record))))
    return {'run_id': plan['run_id'], 'status': 'applied', 'written_claim_ids': written,
            'derived': 'Dirty marker saved; run derived maintenance to refresh browse/search summaries'}


def rollback_run(run_id, *, apply=False):
    from app.db import get_conn
    from app.graph_store import lock_graph_projection
    with get_conn() as conn:
        with conn.cursor() as cur:
            lock_graph_projection(cur, True)
            cur.execute('SELECT * FROM scoped_repair_runs WHERE id=%s::UUID FOR UPDATE', (str(uuid.UUID(run_id)),))
            run = cur.fetchone()
            if not run or run['rolled_back_at']:
                raise ValueError('Repair run missing or already rolled back')
            record, doc_id = run['record'], run['document_id']
            plan = record['plan']
            cids = [c['id'] for c in plan['before']['chunks']]
            cur.execute('SELECT * FROM documents WHERE id=%s FOR UPDATE NOWAIT', (doc_id,))
            document = cur.fetchone()
            if document['status'] in ACTIVE:
                raise ValueError('Document is active')
            if source_state(document, [], [])['document'] != record['after_document']:
                raise ValueError('Document generation changed after repair')
            cur.execute('SELECT * FROM chunks WHERE id=ANY(%s::BIGINT[]) ORDER BY id FOR UPDATE NOWAIT', (cids,))
            chunks = cur.fetchall()
            cur.execute('SELECT * FROM claims WHERE chunk_id=ANY(%s::BIGINT[]) ORDER BY id FOR UPDATE', (cids,))
            if digest(cur.fetchall()) != record['after_claims_hash'] or digest(chunks) != record['after_chunks_hash']:
                raise ValueError('Data changed after repair; refuse to overwrite newer work')
            if any(c['status'] in ACTIVE for c in chunks):
                raise ValueError('A selected chunk is active')
            cur.execute('SELECT * FROM review_items WHERE document_id=%s ORDER BY id FOR UPDATE', (doc_id,))
            if digest(cur.fetchall()) != record['after_reviews_hash']:
                raise ValueError('Reviews changed after repair; preserve newer review decisions')
            if not apply:
                return {'run_id': run_id, 'status': 'rollback_preview', 'restore_claim_ids': [c['id'] for c in plan['before']['claims']]}
            for cid in cids:
                archive_claims(cur, doc_id, cid)
            original_ids = [c['id'] for c in plan['before']['claims']]
            cur.execute('DELETE FROM claims WHERE id=ANY(%s::BIGINT[])', (list(set(original_ids + record['written_ids'])),))
            for chunk in plan['before']['chunks']:
                cur.execute('UPDATE chunks SET text=%s,page_spans=%s,updated_at=%s WHERE id=%s',
                    (chunk['text'], Jsonb(chunk['page_spans']), chunk['updated_at'], chunk['id']))
            _insert_rows(cur, 'claims', plan['before']['claims'])
            # Restore the exact previous projection; no current normalization or
            # semantic inference runs during rollback. Missing referenced graph
            # IDs cause a transaction failure instead of a partial restoration.
            for table in ('facts', 'relation_evidence', 'fact_participants', 'fact_evidence'):
                _insert_rows(cur, table, record['projection'][table])
            cur.execute('DELETE FROM review_items WHERE document_id=%s', (doc_id,))
            _insert_rows(cur, 'review_items', record['before_reviews'])
            cur.execute('UPDATE scoped_repair_runs SET rolled_back_at=now() WHERE id=%s::UUID', (run_id,))
            cur.execute('INSERT INTO derived_dirty_documents(document_id) VALUES (%s) ON CONFLICT(document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1', (doc_id,))
    return {'run_id': run_id, 'status': 'rolled_back'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--document-id', type=int)
    scope.add_argument('--claim-id', type=int, action='append')
    scope.add_argument('--apply-plan', type=Path)
    scope.add_argument('--rollback')
    parser.add_argument('--mode', choices=['repair', 'reproject', 'reextract'], default='repair')
    parser.add_argument('--corrections', type=Path, help='Reviewed exact regions as a JSON array')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply', action='store_true', help='Only applies rollback; planning never writes to the database')
    args = parser.parse_args()
    if args.apply_plan:
        result = apply_plan(json.loads(args.apply_plan.read_text()))
    elif args.rollback:
        result = rollback_run(args.rollback, apply=args.apply)
    else:
        if args.apply:
            parser.error('Create and inspect a plan first, then use --apply-plan')
        from app.db import get_conn
        with get_conn() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor() as cur:
                document, chunks, claims = load_scope(cur, document_id=args.document_id, claim_ids=args.claim_id)
                entries = load_catalog(cur, document['id'], document.get('catalog_version'))
        extractor = None
        if args.mode == 'reextract':
            from app.llm import extract_claims, LLM_MODEL, CLAIMS_PROMPT_VERSION, LLM_REVISION
            extractor = extract_claims
        result = make_plan(document, chunks, claims, entries, mode=args.mode, claim_scope=bool(args.claim_id),
            corrections=json.loads(args.corrections.read_text()) if args.corrections else [], extractor=extractor)
        if extractor:
            result.update(model=LLM_MODEL, prompt_version=CLAIMS_PROMPT_VERSION, model_revision=LLM_REVISION)
    content = json.dumps(json_value(result), ensure_ascii=False, indent=2)
    if args.output:
        # Snapshots contain source text; do not create world-readable artifacts.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(content + '\n')
        print(json.dumps({'output': str(args.output), 'status': result.get('status', 'preview'),
            'run_id': result.get('run_id'), 'diff_counts': {k: len(v) for k,v in result.get('diff', {}).items()}}, ensure_ascii=False))
    else:
        print(content)


if __name__ == '__main__':
    main()
