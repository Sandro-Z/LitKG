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
    return audit_polarity(claim) + audit_certainty(claim)


HEDGE = re.compile(r"\b(?:may(?!(?:\s|,)+\d)|might|could(?! not)|suggest(?:s|ed|ing)?|propos(?:e|es|ed)|(?:is |was |are |were )?thought to|likely|potentially)\b")
UNMEASURED = re.compile(r"\b(?:could not be (?:measured|assessed|evaluated)|(?:not|never) (?:assessed|measured|tested|evaluated)|insufficient\b[^.;]*\b(?:evaluate|assess|test))\b")
NONSIGNIFICANT = re.compile(r'\b(?:non[- ]?significant|not (?:statistically )?significant|no significant (?:difference|change|effect|correlation|association)|did not significantly)\b')
P_VALUE = re.compile(r'\bp\s*(>=|<=|[=><≥≤])\s*((?:0?\.\d+|\d+(?:\.\d+)?)(?:e[-+]?\d+)?)\b')


def _endpoint_scope(claim, quote):
    role = 'subject' if claim.get('predicate') == 'DIFFERS_BETWEEN' else 'object'
    endpoint = _pattern(claim.get(role) or {})
    parts = re.split(r';|\b(?:whereas|however|but(?! not))\b', quote)
    matched = [part for part in parts if re.search(endpoint, part)]
    return matched[0] if len(matched) == 1 else None


def certainty_evidence(claim):
    """Lexical annotations for the unique endpoint arm; no inferred p-values."""
    quote = _text((claim.get('evidence') or {}).get('sentence'))
    scope = _endpoint_scope(claim, quote)
    endpoint = _pattern(claim.get('subject' if claim.get('predicate') == 'DIFFERS_BETWEEN' else 'object') or {})
    hedge = None
    if scope:
        # A hedge about a later mechanism does not negate an earlier measurement.
        hedge = next((m for m in HEDGE.finditer(scope) if re.search(endpoint, scope[m.end():])), None)
        if hedge and re.match(r'\s+due to\b', scope[hedge.end():]) and re.search(rf'{endpoint}[^.;]{{0,100}}\bwhich was observed\b', scope):
            hedge = None
    unavailable = UNMEASURED.search(scope) if scope else None
    # Correlation's two endpoints are symmetric. A non-significant second arm
    # may mention only its taxon; never borrow the positive arm's marker name.
    significance = None
    for m in NONSIGNIFICANT.finditer(quote):
        tail = re.split(r'[.;]|\b(?:whereas|however)\b', quote[m.end():], maxsplit=1)[0]
        if any(re.search(_pattern(claim.get(r) or {}), tail) for r in ('subject','object')):
            significance = m
            break
        if scope and m.group(0) in scope and scope.count('correlation') <= 1:
            significance = m
            break
    ps = [m for m in P_VALUE.finditer(scope or '') if 0 <= float(m[2]) <= 1
          and not re.match(r'\s*(?:[×x*]|[-+]\s*\d)', (scope or '')[m.end():])]
    nonsig_ps = [m for m in ps if (m[1] in {'=','>','>=','≥'} and float(m[2]) > .05) or (m[1] == '>' and float(m[2]) == .05)]
    p_cue = nonsig_ps[0] if ps and len(nonsig_ps) == len(ps) else None
    return {'hedge': hedge.group(0) if hedge else None,
            'unmeasured': unavailable.group(0) if unavailable else None,
            'not_significant': significance.group(0) if significance else p_cue.group(0) if p_cue else None,
            'p_value': float(ps[0][2]) if len(ps) == 1 and ps[0][1] == '=' else None,
            'trend': bool(scope and re.search(r'\btrend\b', scope))}


def audit_certainty(claim):
    cues = certainty_evidence(claim)
    q = claim.get('qualifiers') or {}
    status = claim.get('result_status', q.get('result_status', 'unknown'))
    findings = []
    def flag(rule, cue, field, **extra):
        findings.append({'rule_id': rule, 'cue': cue, 'field': field, 'action': 'needs_review', **extra})
    if status == 'observed_change' and (claim.get('speculative') or cues['hedge']):
        flag('V2-HEDGE-RESULT', 'speculative=true' if claim.get('speculative') else cues['hedge'], 'result_status', recommended_status='unknown')
    endpoint = 'subject' if claim.get('predicate') == 'DIFFERS_BETWEEN' else 'object'
    if cues['unmeasured'] and (status in {'observed_change','no_effect_reported'} or 'unchanged' in _direction(claim, endpoint)):
        flag('V2-UNMEASURED-NULL', cues['unmeasured'], 'result_status', recommended_status='unknown')
    if cues['not_significant'] and status != 'not_significant' and q.get('result_interpretation') != 'not_significant':
        flag('V2-SIGNIFICANCE', cues['not_significant'], 'result_status', recommended_status='not_significant')
    if cues['trend'] and not cues['not_significant'] and status not in {'not_significant','unknown'} and q.get('p_value') is None:
        flag('V2-TREND-SIGNIFICANCE', 'trend', 'qualifiers.p_value')
    scope = _endpoint_scope(claim, _text((claim.get('evidence') or {}).get('sentence')))
    if q.get('result_interpretation') == 'unresolved_negative' and scope and not cues['unmeasured']:
        measured_null = re.search(r"\b(?:doesn't change|didn't change|don't change|does not change|did not affect|unchanged|no change)\b", scope)
        if measured_null:
            flag('V2-RESOLVED-NULL', measured_null.group(0), 'qualifiers.result_interpretation', recommended_status='no_effect_reported')
    return findings


def add_guard_reasons(claim):
    """Use the existing semantic pending/review route for new candidates."""
    for finding in audit_claim(claim.model_dump(mode='json')):
        reason = f"semantic: {finding['rule_id']} field={finding['field']} cue={finding['cue']}"
        if reason not in claim.review_reasons:
            claim.review_reasons.append(reason)
