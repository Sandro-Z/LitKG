"""Preview-first deterministic claim audit; reuse scoped repair history/rollback."""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path

from app.claim_guards import audit_claim


def fixture_claim(row):
    """The review bundle exports fact-shaped rows with endpoints in participants."""
    if isinstance(row.get('claim_json'), dict):
        return copy.deepcopy(row['claim_json'])
    claim = {k: copy.deepcopy(row[k]) for k in ('subject', 'object', 'predicate', 'qualifiers', 'negated', 'speculative', 'result_status') if k in row}
    for p in row.get('participants', []):
        if p.get('role') in {'subject', 'object'}:
            claim[p['role']] = copy.deepcopy(p['entity'])
    claim['participants'] = [{'role':p['role'],'entity':copy.deepcopy(p['entity'])}
                             for p in row.get('participants',[]) if p.get('role') not in {'subject','object'} and isinstance(p.get('entity'),dict)]
    claim['evidence'] = copy.deepcopy(row.get('evidence') or {'sentence': row.get('evidence_text', '')})
    return claim


def summarize(rows):
    rules, docs, hits = Counter(), Counter(), []
    for row in rows:
        findings = audit_claim(fixture_claim(row))
        if findings:
            rules.update({x['rule_id'] for x in findings})
            docs[str(row['document_id'])] += 1
            hits.append({'claim_id': row.get('claim_id', row.get('id')), 'document_id': row['document_id'], 'findings': findings})
    return {'dry_run': True, 'total': len(rows), 'hit_count': len(hits), 'by_rule': dict(sorted(rules.items())),
            'by_document': dict(sorted(docs.items())), 'would_enqueue': len(hits), 'would_downgrade': 0, 'hits': hits}


def make_audit_plan(document, chunks, claims, *, active_claim_ids=()):
    from app.review_repair import ACTIVE, source_state, describe_diff
    from app.catalog import digest
    import uuid
    if document['status'] in ACTIVE or any(c['status'] in ACTIVE for c in chunks):
        raise ValueError('Selected document/chunk is active; retry after processing completes')
    reviews = []
    for c in claims:
        findings = audit_claim(c['claim_json'])
        if findings or c['id'] in active_claim_ids:
            reviews.append({'claim_id': c['id'], 'chunk_id': c['chunk_id'], 'findings': findings, 'active': bool(findings)})
    before = source_state(document, chunks, claims)
    plan = {'schema_version': 1, 'run_id': str(uuid.uuid4()), 'mode': 'audit', 'dry_run': True,
            'document_id': document['id'], 'claim_scope': False, 'before': before, 'before_hash': digest(before),
            'corrections': [], 'changes': [], 'reviews': reviews, 'input_issues': [], 'applicable': True}
    plan['diff'] = describe_diff(plan)
    return plan


def main():
    p = argparse.ArgumentParser(description=__doc__)
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument('--fixtures', type=Path)
    scope.add_argument('--knowledge-base-id', type=int)
    scope.add_argument('--apply-plan', type=Path)
    scope.add_argument('--rollback')
    p.add_argument('--dry-run', action='store_true', help='Default: no database writes')
    p.add_argument('--output', type=Path)
    p.add_argument('--apply', action='store_true', help='Only applies rollback; inspect saved plans before --apply-plan')
    args = p.parse_args()
    if args.dry_run and (args.apply or args.apply_plan):
        p.error('--dry-run cannot be combined with an apply operation')
    if args.apply and not args.rollback:
        p.error('Save and inspect a plan first, then use --apply-plan')
    if args.fixtures:
        result = summarize(json.loads(args.fixtures.read_text()))
    elif args.apply_plan:
        from app.review_repair import apply_plan
        plans = json.loads(args.apply_plan.read_text())['plans']
        if any(plan.get('mode') != 'audit' for plan in plans):
            p.error('Only audit plans are accepted')
        result = {'documents': [apply_plan(plan) for plan in plans]}
    elif args.rollback:
        from app.review_repair import rollback_run
        result = rollback_run(args.rollback, apply=args.apply)
    else:
        from app.db import get_conn
        from app.review_repair import load_scope
        plans, rows = [], []
        with get_conn() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with conn.cursor() as cur:
                cur.execute('SELECT document_id FROM knowledge_base_documents WHERE knowledge_base_id=%s ORDER BY document_id', (args.knowledge_base_id,))
                doc_ids = [d['document_id'] for d in cur.fetchall()]
                for doc_id in doc_ids:
                    document, chunks, claims = load_scope(cur, document_id=doc_id)
                    rows.extend(claims)
                    cur.execute("SELECT detail->>'claim_id' AS claim_id FROM review_items WHERE document_id=%s AND kind='claim_audit' AND detail->>'active'='true'",(doc_id,))
                    active_claim_ids = {int(r['claim_id']) for r in cur.fetchall() if str(r.get('claim_id','')).isdigit()}
                    plan = make_audit_plan(document, chunks, claims, active_claim_ids=active_claim_ids)
                    if plan['reviews']:
                        plans.append(plan)
        result = {**summarize(rows), 'knowledge_base_id': args.knowledge_base_id, 'plans': plans,
                  'would_clear_reviews': sum(not r['active'] for plan in plans for r in plan['reviews'])}
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        import os
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(text + '\n')
        print(json.dumps({k:v for k,v in result.items() if k not in {'plans','hits'}}, ensure_ascii=False))
    else:
        print(text)


if __name__ == '__main__':
    main()
