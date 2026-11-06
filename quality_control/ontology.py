"""Explicit JSON ontology constraints, intentionally not an OWL/SHACL reasoner."""
import json
from collections import defaultdict
from .models import at, finite


def validate(graph):
    findings, checks, failures = [], 0, 0
    nodes = {n['id']: n for n in graph['nodes']}
    ontology = graph['policy'].get('ontology')
    def check(ok, target, rule, message, severity='medium'):
        nonlocal checks, failures
        checks += 1
        if not ok:
            failures += 1
            findings.append(dict(target=target, rule=rule, message=message, severity=severity))
    for c in graph['claims']:
        for role in ('subject', 'object'):
            v = c[role]
            if v['kind'] == 'entity':
                check(v['value'] in nodes, c['id'], 'reference', f'{role} 实体不存在', 'blocker')
    structural_checks = checks
    if not ontology:
        return dict(score=None, checks=checks, violations=failures, findings=findings, available=False)
    types = ontology.get('node_types', ontology.get('types', {}))
    predicates = ontology.get('predicates', {})
    parents = ontology.get('type_parents', {})
    def ancestors(values):
        seen, todo = set(), list(values)
        while todo:
            v = todo.pop()
            if v in seen:
                continue
            seen.add(v)
            parent = parents.get(v, [])
            todo.extend([parent] if isinstance(parent, str) else parent)
        return seen
    def allowed(node, expected):
        return '*' in expected or bool(ancestors(node.get('types', [])) & set(expected))
    def properties(obj, specs, target):
        for name, spec in specs.items():
            if not isinstance(spec, dict):
                continue
            value = at(obj, name)
            present = value is not None and value != '' and value != []
            if spec.get('required'):
                check(present, target, 'required', f'必填字段缺失: {name}')
            if not present:
                continue
            values = value if isinstance(value, list) else [value]
            if 'min_count' in spec:
                check(len(values) >= spec['min_count'], target, 'cardinality', f'{name} 小于最小基数')
            if 'max_count' in spec:
                check(len(values) <= spec['max_count'], target, 'cardinality', f'{name} 超过最大基数')
            if spec.get('unique'):
                check(len({json.dumps(v, sort_keys=True) for v in values}) == len(values), target, 'unique', f'{name} 值重复')
            dtype = spec.get('datatype', spec.get('type'))
            for v in values:
                typed = {'string': isinstance(v, str), 'integer': isinstance(v, int) and not isinstance(v, bool),
                         'number': finite(v, -1e308, 1e308), 'boolean': isinstance(v, bool),
                         'object': isinstance(v, dict), 'array': isinstance(v, list)}
                if dtype:
                    check(typed.get(dtype, False), target, 'datatype', f'{name} 类型应为 {dtype}')
                if 'enum' in spec:
                    check(v in spec['enum'], target, 'enum', f'{name} 超出枚举范围')
                for key, op in [('minimum', lambda a, b: a >= b), ('maximum', lambda a, b: a <= b)]:
                    if key in spec:
                        check(finite(v, -1e308, 1e308) and op(v, spec[key]), target, 'range', f'{name} 超出 {key}')
            if 'units' in spec:
                unit = at(obj, spec.get('unit_field', name + '_unit'))
                check(unit in spec['units'], target, 'unit', f'{name} 单位不符合约束')
    global_unique = defaultdict(dict)
    for n in nodes.values():
        if types:
            check(bool(n.get('types')) and all(t in types for t in n.get('types', [])), n['id'], 'entity_type', '实体类型缺失或未定义')
        for group in ontology.get('disjoint', []):
            check(len(ancestors(n.get('types', [])) & set(group)) <= 1, n['id'], 'disjoint', '实体属于互斥类别')
        specs = dict(ontology.get('node_properties', {}))
        for t in ancestors(n.get('types', [])):
            if isinstance(types, dict) and isinstance(types.get(t), dict):
                specs.update(types[t].get('properties', {}))
                for field in types[t].get('required', []):
                    check(at(n, field) not in (None, '', []), n['id'], 'required', f'必填字段缺失: {field}')
        properties(n, specs, n['id'])
        for field in ontology.get('unique_fields', []):
            value = at(n, field)
            if value is not None:
                key = json.dumps(value, sort_keys=True)
                check(key not in global_unique[field], n['id'], 'unique', f'实体字段 {field} 必须全局唯一')
                global_unique[field][key] = n['id']
    grouped = defaultdict(set)
    for c in graph['claims']:
        p = c['predicate']
        if predicates:
            check(p in predicates, c['id'], 'predicate', '关系未在本体中定义')
        spec = predicates.get(p, {})
        for role, key, litkg in [('subject', 'domain', 'subject_types'), ('object', 'range', 'object_types')]:
            expected = spec.get(key, spec.get(litkg))
            if expected:
                value = c[role]
                node = nodes.get(value['value'], {}) if value['kind'] == 'entity' else {'types': ['Literal']}
                check(allowed(node, expected), c['id'], 'domain_range', f'{p} 的 {role} 类型不匹配')
        for field in spec.get('required', []):
            check(at(c, field) not in (None, '', []), c['id'], 'required', f'必填字段缺失: {field}')
        for field in spec.get('required_qualifiers', []):
            if field in {'product', 'substrate', 'mediator'}:
                value = any(x.get('role') == field for x in c.get('participants', []))
            elif '.' in field:
                value = at(c.get('original', {}).get('claim_json', c.get('original', {})), field)
            else:
                value = c['qualifiers'].get(field)
            check(value not in (None, '', [], False), c['id'], 'required', f'缺少关系限定条件: {field}')
        properties(c, spec.get('properties', {}), c['id'])
        if c['object']['kind'] == 'literal':
            properties(c['object'], {'value': spec['literal']}, c['id']) if spec.get('literal') else None
        # Cardinality counts distinct values, not duplicate evidence records.
        context = json.dumps(c['qualifiers'].get('context'), sort_keys=True)
        grouped[(c['subject']['value'], p, context)].add(json.dumps(c['object'], sort_keys=True))
    for sid, node in nodes.items():
        for p, spec in predicates.items():
            if not isinstance(spec, dict) or not any(k in spec for k in ('min_count', 'max_count', 'unique')):
                continue
            domain = spec.get('domain', spec.get('subject_types', ['*']))
            if not allowed(node, domain):
                continue
            groups = [values for (s, pred, _), values in grouped.items() if s == sid and pred == p] or [set()]
            for values in groups:
                if 'min_count' in spec:
                    check(len(values) >= spec['min_count'], sid, 'cardinality', f'{p} 小于最小基数')
                maximum = 1 if spec.get('unique') else spec.get('max_count')
                if maximum is not None:
                    check(len(values) <= maximum, sid, 'cardinality', f'{p} 超过最大基数')
    ont_checks = checks - structural_checks
    return dict(score=round(100 * (1 - (failures - sum(f['rule'] == 'reference' for f in findings)) / ont_checks), 2) if ont_checks else None,
                checks=checks, violations=failures, findings=findings, available=bool(ont_checks))
