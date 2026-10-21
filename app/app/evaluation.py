"""Local, source-inspectable evaluation; no unlabelled recall claims."""
import argparse
import json
from pathlib import Path

from app.catalog import source_contains
from app.normalization import entity_family, normalize_entity_alias
from app.retrieval import answer_question, retrieve


def evaluate_semantic_cases(cases):
    """Deterministic checks of supplied counterexamples, not a precision metric.

    Evidence and candidate types are fixed fixture inputs. A passing gate says
    nothing about unseen extraction accuracy or a model's entailment ability.
    """
    from app.claims import prepare_claims
    from app.schemas import ClaimBundle, parse_model_bundle
    results = []
    for case in cases:
        if case.get('observable_only'):
            disposition, reasons, claim = 'unassessed', [case['reason']], None
        else:
            bundle = prepare_claims(parse_model_bundle({'claims':[case['candidate']]}, ClaimBundle), case.get('directory', []),
                {'id':1, 'text':case['source_text']}, 'semantic-regression')
            claim = bundle.claims[0] if bundle.claims else None
            disposition = 'rejected' if not claim else claim.semantic_status
            reasons = [r['error'] for r in bundle._claim_rejections] if not claim else claim.review_reasons
        checks = case.get('expected_fields', {})
        actual = claim.model_dump(mode='json') if claim else {}
        fields_match = all(actual.get(key) == value for key,value in checks.items())
        results.append({'name':case['name'], 'provenance':case.get('provenance',{}), 'expected':case['expected'],
            'actual':disposition, 'passed':disposition == case['expected'] and fields_match, 'reasons':reasons,
            'result_status':claim.result_status if claim else None, 'expected_fields_match':fields_match})
    return {'kind':'fixed_evidence_semantic_regression','case_count':len(results),
        'passed_count':sum(r['passed'] and r['actual'] != 'unassessed' for r in results),'failed_count':sum(not r['passed'] for r in results),
        'unassessed_count':sum(r['actual']=='unassessed' for r in results),'cases':results,
        'limitations':['Fixed candidate/source checks; no live extraction or general semantic entailment evaluation.',
                       'Review CSV does not expose original backend types, qualifiers or generation versions.',
                       'No corpus precision, recall or F1 is inferred from these cases.']}


def evaluate(cur, knowledge_base_id, queries=(), generate=False):
    cur.execute("""SELECT DISTINCT e.id,e.canonical_name,e.entity_type,e.normalized_id FROM entities e
        JOIN fact_participants p ON p.entity_id=e.id JOIN facts f ON f.id=p.fact_id
        JOIN claims c ON c.id=f.claim_id JOIN knowledge_base_documents m ON m.document_id=c.document_id
        WHERE m.knowledge_base_id=%s ORDER BY e.id""", (knowledge_base_id,))
    entities = cur.fetchall()
    distribution, names = {}, {}
    for entity in entities:
        distribution[entity['entity_type']] = distribution.get(entity['entity_type'],0) + 1
        key = (entity_family(entity['entity_type']),normalize_entity_alias(entity['canonical_name'],entity['entity_type']))
        names.setdefault(key,[]).append(entity)
    candidates = [rows for rows in names.values() if len(rows)>1]
    report = {'knowledge_base_id':knowledge_base_id,'entity_count':len(entities),
              'type_distribution':distribution,'other_fraction':distribution.get('Other',0)/len(entities) if entities else None,
              'candidate_duplicate_groups':candidates, 'candidate_duplicate_group_count':len(candidates),
              'other_samples':[e for e in entities if e['entity_type']=='Other'][:20],
              'queries':[], 'recall':None, 'citation_error_rate':None,
              'notes':['Same-name groups require review; conflicting IDs may represent different entities.',
                       'Recall requires explicit expected_claim_ids or expected_document_ids; missing labels are not scored.',
                       'Citation checks verify stored provenance, not semantic entailment or the printed journal page.']}
    correct_refs = total_refs = found_labels = total_labels = 0
    for query in queries:
        result = retrieve(cur,knowledge_base_id,query['question'])
        answer = answer_question(result,query['question'],generate)
        found_claims = {c['claim_id'] for c in answer['citations']}
        found_docs = {c['document_id'] for c in answer['citations']}
        expected = set(query.get('expected_claim_ids',[]))
        expected_docs = set(query.get('expected_document_ids',[]))
        found = len(expected & found_claims) + len(expected_docs & found_docs)
        total = len(expected) + len(expected_docs)
        found_labels += found
        total_labels += total
        errors = []
        for ref in answer['citations']:
            total_refs += 1
            cur.execute('''SELECT c.document_id,c.chunk_id,ch.text,d.doi,d.title,d.filename FROM claims c
                JOIN chunks ch ON ch.id=c.chunk_id JOIN documents d ON d.id=c.document_id
                JOIN knowledge_base_documents m ON m.document_id=c.document_id WHERE c.id=%s AND m.knowledge_base_id=%s''',
                (ref['claim_id'],knowledge_base_id))
            source=cur.fetchone()
            valid=bool(source and source['document_id']==ref['document_id'] and source['chunk_id']==ref['chunk_id']
                       and source['doi']==ref['doi'] and source_contains(source['text'],ref['sentence']))
            cur.execute('SELECT evidence FROM fact_evidence WHERE fact_id=%s', (ref['fact_id'],))
            valid=valid and any(e['evidence']['sentence']==ref['sentence'] and e['evidence'].get('page')==ref['page']
                                and e['evidence'].get('page_end')==ref['page_end'] for e in cur.fetchall())
            if valid:
                correct_refs += 1
            else:
                errors.append(ref['id'])
        report['queries'].append({'question':query['question'],'expected_count':total,'recalled_count':found,
                                  'recall':found/total if total else None,'citation_errors':errors,'result':answer})
    if total_labels:
        report['recall']=found_labels/total_labels
    if total_refs:
        report['citation_error_rate']=1-correct_refs/total_refs
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    scope=parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--knowledge-base-id',type=int)
    scope.add_argument('--semantic-cases',type=Path,help='Fixed source/candidate JSON regression cases; no database/model calls')
    parser.add_argument('--queries',type=Path,help='JSON list: question, expected_claim_ids / expected_document_ids')
    parser.add_argument('--baseline',type=Path,help='Earlier local evaluation report')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--generate-answers',action='store_true',help='Explicitly call configured answer LLM')
    args=parser.parse_args()
    if args.semantic_cases:
        report=evaluate_semantic_cases(json.loads(args.semantic_cases.read_text()))
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(f"Semantic regression: {report['passed_count']}/{report['case_count']-report['unassessed_count']} checked cases passed; {report['unassessed_count']} unassessed CSV cases")
        return int(report['failed_count'] != 0)
    from app.db import get_conn
    queries=json.loads(args.queries.read_text()) if args.queries else []
    with get_conn() as conn:
        report=evaluate(conn.cursor(),args.knowledge_base_id,queries,args.generate_answers)
    if args.baseline:
        before=json.loads(args.baseline.read_text())
        report['comparison']={k:{'before':before.get(k),'after':report[k]} for k in
                              ['other_fraction','candidate_duplicate_group_count','recall','citation_error_rate']}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    print(f'Evaluation saved to {args.output}; unlabelled recall remains null')


if __name__ == '__main__':
    raise SystemExit(main())
