"""Lossless import adapters; no implicit negation, identity or completeness guesses."""
import hashlib
import json
import math
from copy import deepcopy

VERSION = 'litkg-qc/1.0.0'
DIMENSIONS = ['specialization', 'granularity', 'completeness', 'correctness', 'ontology', 'coverage']
SCENARIOS = {
    'specific_information': ('提取特定研究信息', [3, 3, 1, 2]),
    'field_review': ('研究领域综述', [3, 3, 2, 3]),
    'paper_understanding': ('深入理解论文', [2, 2, 1, 3]),
    'reproduction': ('复现研究结果', [2, 2, 2, 3]),
    'retrieval': ('相关文献检索', [1, 1, 3, 1]),
    'recommendation': ('文献推荐', [1, 1, 3, 1]),
    'relevance': ('判断文献相关性', [2, 1, 2, 2]),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def finite(value, low=0, high=100):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


def at(value, path, default=None):
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def normalize(payload, name='', mapping=None):
    """Accept canonical, LitKG, SPO, head/relation/tail, triples and mapped records.

    The original input is stored separately by the repository. Every normalized
    row also retains its original fields; unsupported records fail the import.
    """
    original = deepcopy(payload)
    if mapping is not None and (not isinstance(mapping, dict) or
                               any(k not in {'subject', 'predicate', 'object'} or not isinstance(v, str)
                                   for k, v in mapping.items())):
        raise ValueError('字段映射须为 subject/predicate/object 到点号路径的对象')
    root = payload if isinstance(payload, dict) else {}
    body = root.get('graph', root)
    rows = payload if isinstance(payload, list) else next(
        (body[k] for k in ('claims', 'triples', 'relations', 'edges', 'facts') if k in body), None)
    if rows is None and isinstance(body, dict) and any(k in body for k in ('subject', 'head', 's')):
        rows = [body]
    if not isinstance(rows, list):
        raise ValueError('未识别 claim 数组；请使用 claims/triples/edges 或字段映射')
    if len(rows) > 100000:
        raise ValueError('单次导入最多 100000 条 claim')
    nodes, claims, documents = [], [], deepcopy(root.get('documents', []))
    if not isinstance(documents, list):
        raise ValueError('documents 必须是数组')
    if isinstance(body, dict):
        for node in body.get('nodes', body.get('entities', [])):
            if not isinstance(node, dict) or node.get('id') is None:
                raise ValueError('实体必须具有 id')
            n = deepcopy(node)
            n['id'] = str(n['id'])
            n['label'] = str(n.get('label', n.get('name', n.get('text', n['id']))))
            n['types'] = n.get('types') or ([n['type']] if n.get('type') else [])
            n.setdefault('aliases', [])
            if isinstance(n['aliases'], str):
                n['aliases'] = [n['aliases']]
            if not isinstance(n['types'], list) or not all(isinstance(t, str) for t in n['types']):
                raise ValueError('实体类型必须为字符串数组')
            nodes.append(n)
    node_ids = {n['id'] for n in nodes}
    # Explicit IDs preserve distinct identities. Text-only entities are grouped
    # by exact text AND type; case/alias equivalence never merges entities.
    inferred = {}

    def endpoint(value, kind, row):
        if isinstance(value, dict) and value.get('kind') == 'literal':
            return deepcopy(value)
        if isinstance(value, dict) and value.get('kind') == 'entity':
            return dict(deepcopy(value), kind='entity', value=str(value['value']))
        if isinstance(value, dict):
            explicit = value.get('id', value.get('entity_id'))
            label = value.get('label', value.get('text', value.get('name', explicit)))
            types = value.get('types') or ([value['type']] if value.get('type') else [])
        else:
            explicit = None
            label = value
            types = [row[kind + '_type']] if row.get(kind + '_type') else []
            if str(value) in node_ids:
                return {'kind': 'entity', 'value': str(value)}
        if label is None or not str(label).strip():
            raise ValueError('主体或客体为空')
        key = (str(label), tuple(types))
        nid = str(explicit) if explicit is not None else inferred.get(key)
        if nid is None:
            nid = 'text-' + digest(key)[:20]
            inferred[key] = nid
        if nid not in node_ids:
            node = deepcopy(value) if isinstance(value, dict) else {}
            node.update(id=nid, label=str(label), types=types)
            node.setdefault('aliases', [])
            nodes.append(node)
            node_ids.add(nid)
        elif types:
            existing = next(n for n in nodes if n['id'] == nid)
            existing['types'] = list(dict.fromkeys([*existing.get('types', []), *types]))
            if str(label) != existing['label']:
                existing.setdefault('observed_labels', [])
                if str(label) not in existing['observed_labels']:
                    existing['observed_labels'].append(str(label))
        result = {'kind': 'entity', 'value': nid}
        if isinstance(value, dict):
            for field in ('process', 'mention', 'attributes', 'state'):
                if field in value:
                    result[field] = deepcopy(value[field])
        return result

    for i, raw in enumerate(rows):
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            raw = dict(zip(('subject', 'predicate', 'object'), raw))
        if not isinstance(raw, dict):
            raise ValueError(f'第 {i + 1} 条不是有效 claim')
        row = raw.get('claim_json') or raw
        if not isinstance(row, dict):
            raise ValueError('claim_json 必须是对象')
        custom = mapping or {}
        def pick(key, aliases):
            if key in custom:
                return at(raw, custom[key])
            for alias in aliases:
                if alias in row:
                    return row[alias]
                if alias in raw:
                    return raw[alias]
            return None
        subject = pick('subject', ('subject', 'head', 's', 'subject_text', 'source'))
        predicate = pick('predicate', ('predicate', 'relation', 'p', 'label', 'type'))
        obj = pick('object', ('object', 'tail', 'o', 'object_text', 'target'))
        if not isinstance(predicate, str) or not predicate.strip():
            raise ValueError(f'第 {i + 1} 条缺少谓词')
        qualifiers = deepcopy(row.get('qualifiers', raw.get('qualifiers', {})))
        if not isinstance(qualifiers, dict):
            raise ValueError('qualifiers 必须是对象')
        for key in ('negated', 'modality', 'context'):
            if key in row:
                qualifiers.setdefault(key, row[key])
        refs = deepcopy(raw.get('source_refs', row.get('source_refs', []))) or []
        if isinstance(refs, dict):
            refs = [refs]
        if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
            raise ValueError('source_refs 必须为引用对象数组')
        evidence = row.get('evidence') or {}
        if not refs and isinstance(evidence, dict) and evidence.get('sentence'):
            refs = [dict(document_id=raw.get('document_id'), chunk_id=raw.get('chunk_id'), **evidence)]
        cid = str(raw.get('id', row.get('id', f'claim-{i + 1}')))
        claims.append(dict(id=cid, subject=endpoint(subject, 'subject', raw), predicate=predicate,
                           object=endpoint(obj, 'object', raw), qualifiers=qualifiers,
                           participants=deepcopy(row.get('participants', raw.get('participants', []))),
                           source_refs=refs, document_id=str(raw['document_id']) if raw.get('document_id') is not None else None,
                           confidence=raw.get('confidence', row.get('confidence')),
                           original=deepcopy(raw), import_index=i))
    # IDs must be unambiguous for persistent annotations and evidence lookup.
    if len({c['id'] for c in claims}) != len(claims) or len(node_ids) != len(nodes):
        raise ValueError('重复 claim 或实体 ID，无法唯一定位；请先修复输入')
    for doc in documents:
        if not isinstance(doc, dict) or doc.get('id') is None:
            raise ValueError('文献必须有 id')
        doc['id'] = str(doc['id'])
        if not doc.get('blocks') and isinstance(doc.get('text'), str):
            doc['blocks'] = [dict(id='text', text=doc['text'])]
        doc.setdefault('parsing_status', 'partial')
        for index, block in enumerate(doc.get('blocks', [])):
            if not isinstance(block, dict):
                raise ValueError('文本块必须是对象')
            block['id'] = str(block.get('id', f'block-{index + 1}'))
            if not isinstance(block.get('text'), str):
                raise ValueError('文本块缺少 text')
    if len({d['id'] for d in documents}) != len(documents):
        raise ValueError('重复文献 ID')
    for doc in documents:
        blocks = doc.get('blocks', [])
        if len({b['id'] for b in blocks}) != len(blocks):
            raise ValueError('同一文献存在重复文本块 ID')
    policy = deepcopy(root.get('policy', {}))
    if not isinstance(policy, dict):
        raise ValueError('policy 必须是对象')
    policy.setdefault('scope', root.get('scope'))
    policy.setdefault('ontology', root.get('ontology'))
    result = dict(name=name or root.get('name') or '未命名图谱', nodes=nodes, claims=claims,
                  documents=documents, policy=policy, version=VERSION,
                  source_format='mapped' if mapping else 'auto', original_hash=digest(original),
                  adapter_mapping=mapping or {}, metadata=deepcopy(root.get('metadata', {})))
    result['snapshot_hash'] = digest(result)
    return result


def public_claim(claim, nodes):
    c = {k: v for k, v in claim.items() if k not in {'original', 'confidence', 'import_index'}}
    c['subject'] = dict(nodes.get(claim['subject']['value'], {}), **claim['subject'])
    if claim['object']['kind'] == 'entity':
        c['object'] = dict(nodes.get(claim['object']['value'], {}), **claim['object'])
    return c


def blocks(graph):
    return {(d['id'], b['id']): dict(b, doc_id=d['id']) for d in graph['documents'] for b in d.get('blocks', [])}


def anchor(ref, corpus):
    if not isinstance(ref, dict):
        return None
    doc = ref.get('doc_id', ref.get('document_id'))
    block = ref.get('block_id', ref.get('chunk_id'))
    b = corpus.get((str(doc), str(block)))
    if not b:
        return None
    quote = ref.get('quote', ref.get('sentence'))
    if not isinstance(quote, str) or not quote:
        return None
    start, end = ref.get('start'), ref.get('end')
    off = ref.get('offset') or {}
    if start is None:
        start, end = off.get('start'), off.get('end')
    text = b['text']
    if not (isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int)
            and not isinstance(end, bool) and 0 <= start < end <= len(text) and text[start:end] == quote):
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            return None
        end = start + len(quote)
    spans = [s for s in b.get('page_spans', []) if isinstance(s, dict) and
             isinstance(s.get('start'), int) and isinstance(s.get('end'), int)
             and s['start'] < end and s['end'] > start]
    known = spans and all(isinstance(s.get('page_start'), int) and s['page_start'] > 0 and
                          isinstance(s.get('page_end'), int) and s['page_end'] >= s['page_start'] for s in spans)
    page = min(s['page_start'] for s in spans) if known else b.get('page')
    page_end = max(s['page_end'] for s in spans) if known else b.get('page_end', page)
    return dict(doc_id=str(doc), block_id=str(block), start=start, end=end, quote=quote,
                role=ref.get('role', 'context'), page=page, page_end=page_end, section=b.get('section'))
