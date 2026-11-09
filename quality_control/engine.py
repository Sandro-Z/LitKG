"""Independent extraction -> frozen candidates -> fidelity -> omission matching."""
import json
import re
from collections import Counter
from .models import VERSION, anchor, blocks, digest, finite, public_claim
from .sources import review_blocks
from .ontology import validate
from .scoring import scenario_scores
from .prompts import PROMPT_VERSION, EXTRACT, FIDELITY, MATCH, RUBRIC
from .rubrics import measure
from .scoring import SCORING_VERSION
from .llm import LLMError

VERDICTS = {'SUPPORTED', 'PARTIALLY_SUPPORTED', 'CONTRADICTED', 'UNSUPPORTED', 'INSUFFICIENT_EVIDENCE', 'SOURCE_CONFLICT'}
MATCHES = {'matched', 'misrepresented', 'missing', 'ambiguous', 'out_of_scope'}


def evaluate(graph, client=None, checkpoint=lambda report: None, max_chars=48000, *, stop_reason=None):
    corpus = review_blocks(blocks(graph))
    nodes = {n['id']: n for n in graph['nodes']}
    formal = validate(graph)
    report = dict(version=VERSION, prompt_version=PROMPT_VERSION, snapshot_hash=graph['snapshot_hash'],
                  corpus_hash=digest(graph['documents']), policy_hash=digest(graph['policy']),
                  model=getattr(client, 'model', None), assessments=[], candidates=[], matches=[],
                  noun_phrases=[], predicate_mentions=[], negations=[], identity_issues=[],
                  document_coverage=[], errors=[], ontology=formal, stages={}, dimension_assessments=[],
                  limitations=['无人工金标准；候选命中率不是全图召回率或 F1', '只审核已导入文本；图像、表格视觉和未提供附件未核验'])
    if any(d.get("original_source_status") in {"missing", "unreadable", "too_large", "hash_mismatch"} for d in graph["documents"]):
        report["limitations"].append('部分 LitKG 原文件无法读取，相关评估使用已有分块并保留证据缺口。')
    llm = client is not None and client.enabled
    scope = graph['policy'].get('scope')
    docs_complete = bool(graph['documents']) and all(d.get('parsing_status') == 'complete' and d.get('blocks') for d in graph['documents'])
    parsed_claims = [public_claim(c, nodes) for c in graph['claims']]

    last_rejection, rejection_streak = None, 0

    def stopped():
        if stop_reason is not None and not report.get('failure_code'):
            reason = stop_reason()
            if reason:
                report['failure_code'] = reason
        return bool(report.get('failure_code'))

    def ask(stage, data, prompt, *, _recover_truncation=False, **target):
        nonlocal last_rejection, rejection_streak
        if stopped():
            return None
        if len(json.dumps(data, ensure_ascii=False)) > max_chars:
            report['errors'].append(dict(stage=stage, code='CONTEXT_LIMIT', note='未截断原文；该批次保留未审', **target))
            return None
        progress = report.setdefault('progress', {'requests': 0})
        progress['requests'] += 1
        progress['stage'] = stage
        try:
            value = client.ask(prompt, data)
            if not isinstance(value, dict):
                raise LLMError('LLM_INVALID_JSON_OBJECT')
            last_rejection, rejection_streak = None, 0
            return value
        except LLMError as exc:
            # Only the transport's fixed, non-sensitive codes may leave it.
            if _recover_truncation and str(exc) == "LLM_TRUNCATED_RESPONSE":
                raise
            code = str(exc)
            if not re.fullmatch(r'LLM_[A-Z_0-9]+', code):
                code = 'LLM_FAILURE'
            error = dict(stage=stage, code=code, **target)
            if exc.http_400_reason is not None:
                error['reason'] = exc.http_400_reason
            report['errors'].append(error)
            failure_codes = {
                'LLM_HTTP_400': 'LLM_REPEATED_HTTP_400',
                'LLM_TIMEOUT': 'LLM_REPEATED_TIMEOUT',
                'LLM_TRANSPORT_ERROR': 'LLM_REPEATED_TRANSPORT_ERROR',
            }
            if code in failure_codes:
                rejection_streak = rejection_streak + 1 if last_rejection == code else 1
                last_rejection = code
                if rejection_streak >= 3:
                    report['failure_code'] = failure_codes[code]
            else:
                last_rejection, rejection_streak = None, 0
            return None

    def evidence(items, allowed=None, default_role='supports'):
        valid = []
        if isinstance(items, (dict, str)):
            items = [items]
        if not isinstance(items, list):
            return valid
        for item in items:
            if isinstance(item, str):
                item = dict(quote=item)
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item.setdefault('quote', item.get('sentence', item.get('text')))
            item.setdefault('role', default_role)
            item['role'] = {'support': 'supports', 'contradict': 'contradicts', '支持': 'supports', '反证': 'contradicts'}.get(item['role'], item['role'])
            a = anchor(item, corpus)
            if not a and isinstance(item.get('quote'), str) and item['quote'].strip():
                hits = []
                pattern = re.compile(r'\s+'.join(re.escape(w) for w in item['quote'].split()))
                for key, block in corpus.items():
                    if allowed is not None and key not in allowed:
                        continue
                    for found in pattern.finditer(block['text']):
                        hits.append(dict(item, doc_id=key[0], block_id=key[1], start=found.start(), end=found.end(), quote=found.group()))
                        if len(hits) > 1:
                            break
                    if len(hits) > 1:
                        break
                if len(hits) == 1:
                    a = anchor(hits[0], corpus)
                    if a:
                        a['recovered'] = True
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
        while units:
            key, block = units.pop(0)
            if stopped():
                break
            data = dict(scope=scope, blocks=[block])
            try:
                response = ask('extraction', data, EXTRACT, doc_id=key[0], block_id=key[1], _recover_truncation=len(block['text']) > 1600)
            except LLMError:
                _split_text = block["text"]
                _middle = len(_split_text) // 2
                _boundary = _split_text.rfind("\n", len(_split_text) // 3, len(_split_text) * 2 // 3)
                if _boundary >= 0:
                    _middle = _boundary + 1
                _left_end = min(len(_split_text), _middle + 200)
                _right_start = max(0, _middle - 200)
                _offset = block.get('window_start', 0)
                _left = dict(block, text=_split_text[:_left_end], window_start=_offset, window_end=_offset + _left_end)
                _right = dict(block, text=_split_text[_right_start:], window_start=_offset + _right_start, window_end=_offset + len(_split_text))
                units[0:0] = [(key, _left), (key, _right)]
                report["llm_truncation_retries"] = report.get("llm_truncation_retries", 0) + 1
                continue
            ok = response is not None and isinstance(response.get('candidates'), list)
            if response is not None:
                for field in ('candidates', 'noun_phrases', 'predicate_mentions', 'negations'):
                    values = response.get(field, [])
                    if not isinstance(values, list):
                        ok = False
                        continue
                    for item in values:
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
    if not report['noun_phrases']:
        seen = set()
        for candidate in report['candidates']:
            for field in ('subject', 'object'):
                text = candidate[field]
                if text.casefold() not in seen:
                    report['noun_phrases'].append(dict(text=text, evidence=candidate['evidence'], method='anchored_candidate_endpoint'))
                    seen.add(text.casefold())
    report['candidate_hash'] = digest(report['candidates'])
    # Freeze before exposing the graph to any matching request.
    checkpoint(report)

    if llm and not stopped():
        for claim, public in zip(graph['claims'], parsed_claims):
            if stopped():
                break
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
                                           claims=[public], blocks=list(relevant.values())), FIDELITY, claim_id=claim['id'])
            if response is None:
                checkpoint(report)
                continue
            values = response.get('assessments') if response else None
            if values is None and response and 'verdict' in response:
                values = [response]
            valid = isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict) and str(values[0].get('claim_id', claim['id'])) == claim['id']
            a = values[0] if valid else unknown(claim, '模型响应无效或上下文超过预算')
            verdict = str(a.get('verdict', '')).strip().upper()
            refs = [r for r in evidence(a.get('evidence'), set(relevant), 'contradicts' if verdict == 'CONTRADICTED' else 'supports')
                    if r['quote'] in relevant[(r['doc_id'], r['block_id'])]['text']]
            dimension_values = {}
            for field in ('specialization', 'granularity'):
                value = (a.get('rubric') or {}).get(field, a.get(field)) if isinstance(a.get('rubric', {}), dict) else a.get(field)
                dimension_values[field] = measure(value, field, refs) or measure(a.get(field), field, refs)
            if any(v is None for v in dimension_values.values()) and relevant:
                rubric = ask('rubric', dict(scope=scope, claim=public, blocks=list(relevant.values())), RUBRIC, claim_id=claim['id'])
                if rubric and str(rubric.get('claim_id', claim['id'])) == claim['id']:
                    rubric_refs = [r for r in evidence(rubric.get('evidence'), set(relevant))
                                   if r['quote'] in relevant[(r['doc_id'], r['block_id'])]['text']]
                    for field in dimension_values:
                        if dimension_values[field] is None:
                            dimension_values[field] = measure(rubric.get(field), field, rubric_refs)
            report['dimension_assessments'].append(dict(claim_id=claim['id'], **dimension_values))
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
                                  specialization=dimension_values['specialization']['score'] if dimension_values['specialization'] else None,
                                  granularity=dimension_values['granularity']['score'] if dimension_values['granularity'] else None,
                                  searched_blocks=[dict(doc_id=k[0], block_id=k[1]) for k in relevant],
                                  search_notes='已向评审模型提供全部关联文献块' if source_complete else '来源/范围不完整')
            else:
                assessment = unknown(claim, '无效标签、无效证据或不满足判定前置条件')
                report['errors'].append(dict(stage='fidelity', code='INVALID_ASSESSMENT', claim_id=claim['id']))
            report['assessments'].append(assessment)
            checkpoint(report)

    # Matching is a separate model invocation after source candidates are frozen.
    if llm and not stopped():
        for candidate in report['candidates']:
            if stopped():
                break
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
                response = ask('matching', dict(base, claims=batch, graph_complete=len(batches) == 1), MATCH, candidate_id=candidate['id'])
                if stopped():
                    break
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
            if stopped():
                break
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
    report['stages']['rubric'] = 'complete' if len(report['dimension_assessments']) == len(graph['claims']) and graph['claims'] and all(
        r.get(field) and r[field]['coverage'] == 1 for r in report['dimension_assessments'] for field in ('specialization', 'granularity')) else 'incomplete'
    report['stages']['matching'] = 'complete' if llm and len(report['matches']) == len(report['candidates']) and not any(m['status'] == 'ambiguous' for m in report['matches']) else 'incomplete'
    stopped()
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
        records = {a['claim_id']: a[field] for a in assessments if a.get(field) is not None}
        for row in report.get('dimension_assessments', []):
            if row.get(field):
                records[row['claim_id']] = row[field]['score']
        values = list(records.values())
        if values:
            dims[field] = round(sum(values) / len(values), 2)
    if determined:
        # Strict support rate, includes partial and unresolved outcomes in R.
        dims['correctness'] = round(100 * labels['SUPPORTED'] / len(assessments), 2)
    match_counts = Counter(m['status'] for m in report['matches'])
    denominator = sum(match_counts[k] for k in ('matched', 'misrepresented', 'missing'))
    if denominator:
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
    incomplete = (bool(report.get('failure_code')) or not count or len(assessments) != count or unresolved > 0 or not scope_known or not docs_complete
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
    details = {}
    for field in ('specialization', 'granularity'):
        scored = {a['claim_id'] for a in assessments if a.get(field) is not None}
        scored.update(r['claim_id'] for r in report.get('dimension_assessments', []) if r.get(field))
        details[field] = dict(method='evidence_rubric_mean', evaluated=len(scored), total=count,
                              coverage=len(scored) / count if count else None,
                              reason='原文支持的逐claim量表均值' if scored else '没有通过证据校验的量表评分；查看rubric/fidelity阶段错误与原文锚点')
    details['correctness'] = dict(method='strict_supported_fraction', numerator=labels['SUPPORTED'], denominator=len(assessments),
                                  evaluated=determined, total=count, coverage=determined / count if count else None,
                                  reason='严格支持数 / 全部已审，部分支持和未定单列' if determined else '尚无可判定的原文核查结果；缺原文、模型响应或引用未通过校验')
    details['completeness'] = dict(method='source_candidate_hit_rate', numerator=match_counts['matched'], denominator=denominator,
                                   evaluated=denominator, total=len(report['candidates']),
                                   coverage=denominator / len(report['candidates']) if report['candidates'] else None,
                                   basis='declared_extraction_scope' if scope_known else 'provided_source_text_only',
                                   reason=('范围内候选命中率' if scope_known else '未声明抽取范围：仅报告已提供原文的候选覆盖暂定分，不视为全图召回率')
                                   if denominator else '没有已锚定并完成匹配的原文候选；检查独立重抽取与匹配阶段')
    details['coverage'] = dict(method='available_proxy_mean', evaluated=len(measures), total=4, coverage=len(measures) / 4,
                               components=dict(noun_phrases=noun_score, predicate_preservation=relation_score,
                                               negation_preservation=neg_score, relation_density=density),
                               reason='可用代理指标的均值；不适用或未完成的子指标不补零' if measures else '未取得有效的名词短语或原文候选，无法计算覆盖代理')
    details['ontology'] = dict(method='explicit_constraint_pass_rate', evaluated=report['ontology']['checks'],
                               reason='显式约束检测' if dims['ontology'] is not None else '未提供可执行的本体约束')
    for field, detail in details.items():
        detail['score'] = dims[field]
        detail['status'] = 'unavailable' if dims[field] is None else ('complete' if not incomplete else 'partial')
    report['dimension_details'] = details
    report['scoring_version'] = SCORING_VERSION
    report['failure_summary'] = dict(Counter(e['code'] for e in report['errors']))
    report['evaluation_status'] = 'complete' if not incomplete else 'completed_with_gaps'
