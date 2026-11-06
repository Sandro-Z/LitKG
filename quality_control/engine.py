"""Independent extraction -> frozen candidates -> fidelity -> omission matching."""
import json
import re
from collections import Counter
from .models import VERSION, anchor, blocks, digest, finite, public_claim
from .ontology import validate
from .scoring import scenario_scores
from .prompts import PROMPT_VERSION, EXTRACT, FIDELITY, MATCH
from .llm import LLMError

VERDICTS = {'SUPPORTED', 'PARTIALLY_SUPPORTED', 'CONTRADICTED', 'UNSUPPORTED', 'INSUFFICIENT_EVIDENCE', 'SOURCE_CONFLICT'}
MATCHES = {'matched', 'misrepresented', 'missing', 'ambiguous', 'out_of_scope'}


def evaluate(graph, client=None, checkpoint=lambda report: None, max_chars=48000):
    corpus = blocks(graph)
    nodes = {n['id']: n for n in graph['nodes']}
    formal = validate(graph)
    report = dict(version=VERSION, prompt_version=PROMPT_VERSION, snapshot_hash=graph['snapshot_hash'],
                  corpus_hash=digest(graph['documents']), policy_hash=digest(graph['policy']),
                  model=getattr(client, 'model', None), assessments=[], candidates=[], matches=[],
                  noun_phrases=[], predicate_mentions=[], negations=[], identity_issues=[],
                  document_coverage=[], errors=[], ontology=formal, stages={},
                  limitations=['无人工金标准；候选命中率不是全图召回率或 F1', '只审核已导入文本；图像、表格视觉和未提供附件未核验'])
    llm = client is not None and client.enabled
    scope = graph['policy'].get('scope')
    docs_complete = bool(graph['documents']) and all(d.get('parsing_status') == 'complete' and d.get('blocks') for d in graph['documents'])
    parsed_claims = [public_claim(c, nodes) for c in graph['claims']]

    def ask(stage, data, prompt):
        if len(json.dumps(data, ensure_ascii=False)) > max_chars:
            report['errors'].append(dict(stage=stage, code='CONTEXT_LIMIT', note='未截断原文；该批次保留未审'))
            return None
        try:
            value = client.ask(prompt, data)
            if not isinstance(value, dict):
                raise LLMError('LLM_INVALID_JSON_OBJECT')
            return value
        except LLMError as exc:
            # Only the transport's fixed, non-sensitive codes may leave it.
            code = str(exc)
            if not re.fullmatch(r'LLM_[A-Z_0-9]+', code):
                code = 'LLM_FAILURE'
            report['errors'].append(dict(stage=stage, code=code))
            return None

    def evidence(items, allowed=None):
        valid = []
        if not isinstance(items, list):
            return valid
        for item in items:
            a = anchor(item, corpus)
            if a and (allowed is None or (a['doc_id'], a['block_id']) in allowed):
                if a['role'] in {'supports', 'contradicts', 'context'}:
                    valid.append(a)
        return valid

    def unknown(c, reason):
        return dict(claim_id=c['id'], verdict='INSUFFICIENT_EVIDENCE', rationale=reason,
                    evidence=[], specialization=None, granularity=None, searched_blocks=[])

    # Source-only requests cannot contain graph labels, confidence or conclusions.
    if llm:
        units = []
        window_size = min(12000, max(1000, max_chars // 2))
        for key, original in corpus.items():
            text = original['text']
            start = 0
            while start < len(text):
                end = min(len(text), start + window_size)
                if end < len(text):
                    boundary = text.rfind(' ', start + window_size // 2, end)
                    if boundary > start:
                        end = boundary
                units.append((key, dict(original, text=text[start:end], window_start=start, window_end=end)))
                if end == len(text):
                    break
                start = max(start + 1, end - 200)
            if not text:
                units.append((key, dict(original, window_start=0, window_end=0)))
        for key, block in units:
            data = dict(scope=scope, blocks=[block])
            response = ask('extraction', data, EXTRACT)
            ok = response is not None and all(isinstance(response.get(k), list) for k in
                                             ('candidates', 'noun_phrases', 'predicate_mentions', 'negations'))
            if ok:
                for field in ('candidates', 'noun_phrases', 'predicate_mentions', 'negations'):
                    for item in response[field]:
                        if not isinstance(item, dict):
                            ok = False
                            continue
                        refs = [r for r in evidence(item.get('evidence'), {key}) if r['quote'] in block['text']]
                        required = ('subject', 'predicate', 'object') if field == 'candidates' else ('text',)
                        if not refs or not all(isinstance(item.get(k), str) and item[k].strip() for k in required):
                            ok = False
                            continue
                        entry = {k: item[k] for k in required}
                        entry['evidence'] = refs
                        if field == 'candidates':
                            if not isinstance(item.get('qualifiers', {}), dict):
                                ok = False
                                continue
                            entry['qualifiers'] = item.get('qualifiers', {})
                            entry['id'] = 'candidate-' + digest({k: v for k, v in entry.items() if k != 'evidence'})[:24]
                        duplicate = next((c for c in report[field] if
                                          (c.get('id') == entry.get('id') if field == 'candidates'
                                           else c['text'].casefold() == entry['text'].casefold())), None)
                        if duplicate:
                            duplicate['evidence'].extend(r for r in refs if r not in duplicate['evidence'])
                        else:
                            report[field].append(entry)
            report['document_coverage'].append(dict(doc_id=key[0], block_id=key[1], purpose='omission', reviewed=ok,
                                                     block_hash=digest(corpus[key]['text']),
                                                     ranges=[[block['window_start'], block['window_end']]]))
            if not ok:
                report['errors'].append(dict(stage='extraction', code='INVALID_OR_UNREVIEWED_BLOCK', doc_id=key[0], block_id=key[1]))
            checkpoint(report)
        merged = {}
        for item in report['document_coverage']:
            key = (item['doc_id'], item['block_id'])
            if key not in merged:
                merged[key] = item
            else:
                merged[key]['reviewed'] = merged[key]['reviewed'] and item['reviewed']
                merged[key]['ranges'].extend(item['ranges'])
        report['document_coverage'] = list(merged.values())
    report['stages']['extraction'] = 'complete' if llm and corpus and all(x['reviewed'] for x in report['document_coverage']) else 'incomplete'
    report['candidate_hash'] = digest(report['candidates'])
    # Freeze before exposing the graph to any matching request.
    checkpoint(report)

    if llm:
        for claim, public in zip(graph['claims'], parsed_claims):
            refs = claim.get('source_refs', [])
            doc_ids = {str(r.get('doc_id', r.get('document_id'))) for r in refs if isinstance(r, dict)
                       and r.get('doc_id', r.get('document_id')) is not None}
            if claim.get('document_id'):
                doc_ids.add(claim['document_id'])
            relevant = {k: b for k, b in corpus.items() if not doc_ids or k[0] in doc_ids}
            source_complete = bool(relevant) and bool(doc_ids) and doc_ids <= {d['id'] for d in graph['documents']} and all(
                d.get('parsing_status') == 'complete' for d in graph['documents'] if d['id'] in doc_ids)
            if not relevant:
                report['assessments'].append(unknown(claim, '缺少来源文本'))
                continue
            full_data = dict(scope=scope, source_complete=source_complete,
                             claims=[public], blocks=list(relevant.values()))
            if len(json.dumps(full_data, ensure_ascii=False)) > max_chars:
                # Recover source anchors first, then lexical hits and neighbours.
                # This is evidence discovery, never an UNSUPPORTED decision.
                keys = list(relevant)
                referenced = {(str(r.get('doc_id', r.get('document_id'))),
                               str(r.get('block_id', r.get('chunk_id'))))
                              for r in claim['source_refs'] if isinstance(r, dict)}
                terms = [str(public[role].get('label', public[role].get('value', ''))).casefold()
                         for role in ('subject', 'object')]
                ranked = sorted(keys, key=lambda k: (k in referenced,
                                sum(term in relevant[k]['text'].casefold() for term in terms if term)), reverse=True)
                picked = {}
                for k in ranked:
                    for candidate_key in [k, *keys[max(0, keys.index(k)-1):keys.index(k)+2]]:
                        trial = dict(picked)
                        view = relevant[candidate_key]
                        if len(view['text']) > max_chars // 2:
                            text = view['text']
                            positions = [a['start'] for ref in claim['source_refs']
                                         if (a := anchor(ref, corpus)) and (a['doc_id'], a['block_id']) == candidate_key]
                            if not positions:
                                positions = [text.casefold().find(term) for term in terms if term and term in text.casefold()]
                            start = max(0, min(positions, default=0) - 2000)
                            end = min(len(text), start + max(1000, max_chars // 2))
                            view = dict(view, text=text[start:end], window_start=start, window_end=end)
                        trial[candidate_key] = view
                        data_size = len(json.dumps(dict(scope=scope, source_complete=False,
                                                       claims=[public], blocks=list(trial.values())), ensure_ascii=False))
                        if data_size <= max_chars:
                            picked = trial
                relevant = picked
                source_complete = False
                report['limitations'].append('声明 ' + claim['id'] + ' 使用锚点/词项与邻块检索；未穷尽关联全文，禁止判 UNSUPPORTED')
            response = ask('fidelity', dict(scope=scope, source_complete=source_complete,
                                           claims=[public], blocks=list(relevant.values())), FIDELITY)
            values = response.get('assessments') if response else None
            valid = isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict) and values[0].get('claim_id') == claim['id']
            a = values[0] if valid else unknown(claim, '模型响应无效或上下文超过预算')
            refs = [r for r in evidence(a.get('evidence'), set(relevant))
                    if r['quote'] in relevant[(r['doc_id'], r['block_id'])]['text']]
            verdict = a.get('verdict')
            roles = {r['role'] for r in refs}
            valid = valid and verdict in VERDICTS and isinstance(a.get('rationale'), str) and bool(a['rationale'].strip())
            if verdict in {'SUPPORTED', 'PARTIALLY_SUPPORTED'}:
                valid = valid and 'supports' in roles
            if verdict == 'CONTRADICTED':
                valid = valid and 'contradicts' in roles
            if verdict == 'SOURCE_CONFLICT':
                valid = valid and {'supports', 'contradicts'} <= roles and len({r['doc_id'] for r in refs}) >= 2
            if verdict == 'UNSUPPORTED':
                valid = valid and source_complete
            if valid:
                assessment = dict(claim_id=claim['id'], verdict=verdict, rationale=a['rationale'][:6000], evidence=refs,
                                  specialization=a.get('specialization') if refs and finite(a.get('specialization')) else None,
                                  granularity=a.get('granularity') if refs and finite(a.get('granularity')) else None,
                                  searched_blocks=[dict(doc_id=k[0], block_id=k[1]) for k in relevant],
                                  search_notes='已向评审模型提供全部关联文献块' if source_complete else '来源/范围不完整')
            else:
                assessment = unknown(claim, '无效标签、无效证据或不满足判定前置条件')
                report['errors'].append(dict(stage='fidelity', code='INVALID_ASSESSMENT', claim_id=claim['id']))
            report['assessments'].append(assessment)
            checkpoint(report)

    # Matching is a separate model invocation after source candidates are frozen.
    if llm:
        for candidate in report['candidates']:
            refs = candidate['evidence'][:3]
            source_views = {}
            for ref in refs:
                key = (ref['doc_id'], ref['block_id'])
                block = corpus[key]
                start = max(0, ref['start'] - 1000)
                end = min(len(block['text']), max(ref['end'] + 1000, start + 3000))
                source_views[key] = dict(block, text=block['text'][start:end], window_start=start, window_end=end)
            base = dict(scope=scope, candidates=[dict(candidate, evidence=refs)], graph_complete=False,
                        blocks=list(source_views.values()))
            batches, batch = [], []
            for claim in parsed_claims:
                if batch and (len(batch) >= 50 or len(json.dumps(dict(base, claims=batch + [claim]), ensure_ascii=False)) > max_chars):
                    batches.append(batch)
                    batch = []
                batch.append(claim)
            batches.append(batch)
            batch_matches, all_issues = [], []
            for batch in batches:
                response = ask('matching', dict(base, claims=batch, graph_complete=len(batches) == 1), MATCH)
                values = response.get('matches') if response else None
                value = values[0] if isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict) else {}
                batch_ids = {c['id'] for c in batch}
                ids = value.get('claim_ids', [])
                status = value.get('status')
                valid = (value.get('candidate_id') == candidate['id'] and status in MATCHES | {'not_found_in_batch'} and isinstance(ids, list)
                         and all(isinstance(x, str) and x in batch_ids for x in ids))
                if status in {'matched', 'misrepresented'}:
                    valid = valid and bool(ids)
                if status in {'missing', 'out_of_scope', 'not_found_in_batch'}:
                    valid = valid and not ids
                if not valid:
                    value = dict(candidate_id=candidate['id'], status='ambiguous', claim_ids=[], rationale='该图谱批次匹配未完成或响应无效')
                batch_matches.append(value)
                issues = response.get('identity_issues', []) if response else []
                if isinstance(issues, list):
                    all_issues.extend(issues)
            # Missing can only be established after ALL graph batches have
            # explicitly excluded a representation. Ambiguity stays unresolved.
            aligned = [m for m in batch_matches if m['status'] in {'matched', 'misrepresented'}]
            if aligned:
                status = 'matched' if any(m['status'] == 'matched' for m in aligned) else 'misrepresented'
                match = dict(candidate_id=candidate['id'], status=status,
                             claim_ids=list(dict.fromkeys(i for m in aligned if m['status'] == status for i in m['claim_ids'])),
                             rationale='；'.join(str(m.get('rationale', '')) for m in aligned)[:6000])
            elif all(m['status'] == 'out_of_scope' for m in batch_matches):
                match = dict(candidate_id=candidate['id'], status='out_of_scope', claim_ids=[], rationale='候选超出抽取范围')
            elif all(m['status'] in {'missing', 'not_found_in_batch'} for m in batch_matches):
                match = dict(candidate_id=candidate['id'], status='missing', claim_ids=[], rationale='已遍历全部图谱批次，未发现对应声明')
            else:
                match = dict(candidate_id=candidate['id'], status='ambiguous', claim_ids=[], rationale='图谱分批匹配仍有未定项，不判定漏抽')
            ids = match.get('claim_ids', [])
            status = match.get('status')
            valid = (match.get('candidate_id') == candidate['id'] and status in MATCHES and isinstance(ids, list)
                     and all(isinstance(x, str) and x in {c['id'] for c in graph['claims']} for x in ids))
            if status in {'matched', 'misrepresented'}:
                valid = valid and bool(ids)
            if status in {'missing', 'out_of_scope'}:
                valid = valid and not ids
            if not valid:
                match = dict(candidate_id=candidate['id'], status='ambiguous', claim_ids=[], rationale='匹配未完成或响应无效')
                report['errors'].append(dict(stage='matching', code='INVALID_MATCH', candidate_id=candidate['id']))
            else:
                match = dict(candidate_id=candidate['id'], status=status, claim_ids=ids,
                             rationale=str(match.get('rationale', ''))[:6000])
            report['matches'].append(match)
            for issue in all_issues:
                if not isinstance(issue, dict):
                    continue
                refs = [r for r in evidence(issue.get('evidence'), set(source_views))
                        if r['quote'] in source_views[(r['doc_id'], r['block_id'])]['text']]
                nids = issue.get('node_ids', [])
                if refs and issue.get('kind') in {'overmerged', 'oversplit'} and isinstance(nids, list) and nids and all(i in nodes for i in nids):
                    value = dict(kind=issue['kind'], node_ids=nids, rationale=str(issue.get('rationale', ''))[:6000], evidence=refs, status='hypothesis')
                    if value not in report['identity_issues']:
                        report['identity_issues'].append(value)
            checkpoint(report)
    report['stages']['fidelity'] = 'complete' if llm and len(report['assessments']) == len(graph['claims']) else 'incomplete'
    report['stages']['matching'] = 'complete' if llm and len(report['matches']) == len(report['candidates']) and not any(m['status'] == 'ambiguous' for m in report['matches']) else 'incomplete'
    finalize(graph, report, docs_complete, bool(scope))
    report['llm_calls'] = getattr(client, 'calls', 0)
    report['usage'] = getattr(client, 'usage', {})
    return report


def finalize(graph, report, docs_complete, scope_known):
    count = len(graph['claims'])
    assessments = report['assessments']
    labels = Counter(a['verdict'] for a in assessments)
    determined = sum(labels[k] for k in ('SUPPORTED', 'PARTIALLY_SUPPORTED', 'CONTRADICTED', 'UNSUPPORTED'))
    unresolved = labels['INSUFFICIENT_EVIDENCE'] + labels['SOURCE_CONFLICT']
    dims = dict(specialization=None, granularity=None, correctness=None, completeness=None,
                ontology=report['ontology']['score'], coverage=None)
    for field in ('specialization', 'granularity'):
        values = [a[field] for a in assessments if a[field] is not None]
        if values:
            dims[field] = round(sum(values) / len(values), 2)
    if determined:
        # Strict support rate, includes partial and unresolved outcomes in R.
        dims['correctness'] = round(100 * labels['SUPPORTED'] / len(assessments), 2)
    match_counts = Counter(m['status'] for m in report['matches'])
    denominator = sum(match_counts[k] for k in ('matched', 'misrepresented', 'missing'))
    if denominator and scope_known:
        dims['completeness'] = round(100 * match_counts['matched'] / denominator, 2)
    names = [str(v).casefold() for n in graph['nodes'] for v in [n['label'], *n.get('aliases', [])]]
    def represented(text):
        text = text.casefold().strip()
        return any(text == name or (len(name) >= 3 and name in text) for name in names)
    phrases = report['noun_phrases']
    noun_hit = sum(represented(p['text']) for p in phrases)
    noun_score = 100 * noun_hit / len(phrases) if phrases else None
    relation_score = 100 * match_counts['matched'] / denominator if denominator else None
    candidate_neg = {c['id'] for c in report['candidates'] if c.get('qualifiers', {}).get('negated') is True}
    neg_matches = [m for m in report['matches'] if m['candidate_id'] in candidate_neg]
    neg_score = 100 * sum(m['status'] == 'matched' for m in neg_matches) / len(neg_matches) if neg_matches else None
    comparable = bool(scope_known and docs_complete and report['stages']['extraction'] == 'complete')
    unique_claims = {digest({k: c[k] for k in ('subject', 'predicate', 'object', 'qualifiers')}) for c in graph['claims']}
    nc, ng = len(report['candidates']), len(unique_claims)
    ratio = ng / nc if comparable and nc else None
    density = 100 * min(ratio, 1 / ratio) if ratio and ratio > 0 else (0 if ratio == 0 else None)
    measures = [v for v in (noun_score, relation_score, neg_score, density) if v is not None]
    if measures:
        dims['coverage'] = round(sum(measures) / len(measures), 2)
    incomplete = (not count or len(assessments) != count or unresolved > 0 or not scope_known or not docs_complete
                  or any(v != 'complete' for v in report['stages'].values()) or bool(report['errors'])
                  or bool(report['identity_issues']) or any(dims[k] is None for k in dims))
    blocker = any(f['severity'] == 'blocker' for f in report['ontology']['findings'])
    errors = (labels['CONTRADICTED'] + labels['UNSUPPORTED'] + labels['PARTIALLY_SUPPORTED']
              + report['ontology']['violations']
              + (match_counts['missing'] + match_counts['misrepresented'] if scope_known else 0))
    gate = 'FAIL' if blocker else ('NEEDS_REVISION' if errors else ('INCOMPLETE' if incomplete else 'PASS'))
    report.update(dimensions=dims, gate=gate, complete=not incomplete,
                  scenarios=scenario_scores(dims, not incomplete, blocker),
                  metrics=dict(claim_count=count, reviewed=len(assessments), labels=dict(labels),
                               claim_coverage=len(assessments) / count if count else None,
                               undetermined_rate=unresolved / len(assessments) if assessments else None,
                               strict_support_rate=labels['SUPPORTED'] / len(assessments) if assessments else None,
                               candidate_counts=dict(match_counts), candidate_denominator=denominator,
                               scope_known=scope_known, source_complete=docs_complete),
                  proxies=dict(noun_phrases=dict(total=len(phrases), represented=noun_hit, score=noun_score),
                               predicate_preservation=relation_score, negation_preservation=neg_score,
                               predicate_mentions=len(report['predicate_mentions']), negation_mentions=len(report['negations']),
                               relation_count_ratio=ratio, relation_density_score=density,
                               identity_issues=len(report['identity_issues'])))
    report['uncertainty'] = max(1 - len(assessments) / count if count else 1,
                                unresolved / len(assessments) if assessments else 1,
                                .5 if incomplete else 0)
