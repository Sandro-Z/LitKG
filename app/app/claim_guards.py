"""Deterministic, argument-scoped audit rules. Input assertions are never edited."""
import re
import unicodedata


def audit_pending_sql(alias):
    """An audit conclusion is separate from the immutable extracted assertion."""
    return f"""EXISTS (SELECT 1 FROM review_items audit WHERE audit.document_id={alias}.document_id
        AND audit.kind='claim_audit' AND audit.detail->>'claim_id'={alias}.id::TEXT
        AND audit.detail->>'active'='true')"""


def _text(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub(r'\s+', ' ', value.translate(str.maketrans({'—': '-', '–': '-', '−': '-', '’': "'"}))).strip()


def _names(entity):
    return list(dict.fromkeys(_text(entity.get(k)) for k in ('mention', 'text') if entity.get(k)))


def _pattern(entity):
    names = _names(entity)
    return r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r')(?!\w)' if names else r'(?!)'


def _direction(claim, role):
    q = claim.get('qualifiers') or {}
    values = [(claim.get(role, {}).get('process') or {}).get('direction')]
    values += [p.get('direction') for p in q.get('processes', []) if isinstance(p, dict) and p.get('role') == role]
    values.append(q.get('direction' if role == 'subject' and claim.get('predicate') == 'DIFFERS_BETWEEN' else role + '_direction'))
    return {v for v in values if v and v != 'unspecified'}


def audit_polarity(claim):
    """Return only lexical conflicts with named arguments, never infer new edges."""
    quote = _text((claim.get('evidence') or {}).get('sentence'))
    s, o = claim.get('subject') or {}, claim.get('object') or {}
    sp, op = _pattern(s), _pattern(o)
    predicate, negated = claim.get('predicate'), claim.get('negated', False)
    findings = []

    def flag(rule, match, field):
        if match:
            findings.append({'rule_id': rule, 'cue': match.group(0), 'field': field, 'action': 'needs_review'})

    if predicate in {'AFFECTS', 'ACTIVATES', 'INHIBITS'}:
        od, sd = _direction(claim, 'object'), _direction(claim, 'subject')
        if 'decreased' in od and any(re.search(r'\banti-', n) for n in _names(o)):
            flag('V1-ANTI-ATTRIBUTE', re.search(rf'\b(?:has|have|possesses|are|is)\b[^.;]{{0,90}}?{op}', quote), 'object.process.direction')
        # A bare mediator is not the inhibition/absence event in the source.
        if not sd and not re.search(r'\b(?:inhibition|absence|lack|loss)\b', ' '.join(_names(s))):
            flag('V1-SUBJECT-OPERATOR', re.search(rf'\b(?:inhibition of|inhibitory (?:action|effects?) on|absence of|lack of|loss of)\s+{sp}', quote), 'subject')
        if 'decreased' in sd and re.search(r'\binhibition\b', ' '.join(_names(s))):
            flag('V1-EVENT-OPERATOR', re.search(rf'{sp}[^.;]{{0,35}}?\b(?:leads? to|caus\w*)\b', quote), 'subject.process.direction')
        flag('V1-PASSIVE-AGENT', re.search(rf'{sp}\s+(?:was|were|is|are)\s+protected against\b', quote), 'subject')
        match = re.search(rf'{sp}\s+(?:is|was|are|were)\s+sensitive to\b[^.;]*{op}', quote)
        flag('V1-SENSITIVITY-AGENT', match, 'subject')

    endpoint = 'subject' if predicate == 'DIFFERS_BETWEEN' else 'object'
    positive = not negated and claim.get('result_status') not in {'no_effect_reported', 'not_significant'} and 'unchanged' not in _direction(claim, endpoint)
    if positive:
        # Exception members must be named in the excluded span, not elsewhere.
        for match in re.finditer(r'\ball but\s+(?:\w+)\s*\(([^)]+)\)', quote):
            if re.search(sp, match[1]) or re.search(op, match[1]):
                flag('V1-EXCEPTION', match, 'negated')
        for match in re.finditer(r'\b(?:but not|rather than|except(?: for)?)\s+([^.;,\-]+)', quote):
            if re.search(op, match[1]):
                flag('V1-EXCLUDED-OBJECT', match, 'object')
        if predicate in {'CORRELATED_WITH', 'ASSOCIATED_WITH'} and re.search(sp, quote) and re.search(op, quote):
            flag('V1-NO-RELATION', re.search(r'\bno (?:relationship|association|correlation)\b[^.;]*\b(?:identified|found|observed|detected|established)\b', quote), 'negated')
        if predicate == 'PREDICTS' and re.search(sp, quote) and re.search(op, quote):
            flag('V1-UNASSESSED-PREDICTION', re.search(r'\binsufficient\b[^.;]*\b(?:evaluate|assess|test)\b', quote), 'predicate')
        verbs = {'CONSUMES': r'(?:consum\w*|utiliz\w*|use)', 'PRODUCES': r'(?:produc\w*|synthesiz\w*)',
                 'TRANSFORMS': r'(?:transform\w*|convert\w*|metaboliz\w*)', 'PREDICTS': r'predict\w*'}
        if predicate in verbs:
            flag('V1-NEGATED-PREDICATE', re.search(rf'{sp}\s+(?:(?:did|does|do|could) not|cannot|failed to|fails to|unable to)\s+{verbs[predicate]}[^.;]*{op}', quote), 'negated')
        if predicate == 'PART_OF':
            flag('V1-CONTAINS-REVERSED', re.search(rf'{sp}\s+contain(?:s|ing)?\b[^.;]{{0,25}}?{op}', quote), 'predicate')
            flag('V1-CONTAINER-REVERSED', re.search(rf'{op}\s+in\s+{sp}', quote), 'predicate')
    return findings


def audit_claim(claim):
    return audit_polarity(claim)


def add_guard_reasons(claim):
    """Use the existing semantic pending/review route for new candidates."""
    for finding in audit_claim(claim.model_dump(mode='json')):
        reason = f"semantic: {finding['rule_id']} field={finding['field']} cue={finding['cue']}"
        if reason not in claim.review_reasons:
            claim.review_reasons.append(reason)
