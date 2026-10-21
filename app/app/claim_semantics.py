"""Conservative result semantics and candidate-local checks, without model calls.

Rules flag risks; they do not prove entailment or manufacture a new relation.
Original wording is always retained in evidence and repair history.
"""
import re

from app.catalog import source_contains


NOT_SIGNIFICANT = re.compile(r'\b(?:not statistically significant|no significant (?:difference|change|effect)|did not significantly|not significantly (?:different|alter|affect|change))\b', re.I)
NO_EFFECT = re.compile(r'\b(?:did not (?:affect|alter|change)|had no effect on|failed to (?:affect|influence)|remained unchanged|was unchanged|were unchanged)\b', re.I)
CAUSAL = {'CAUSES', 'TREATS', 'PREVENTS', 'AFFECTS', 'ACTIVATES', 'INHIBITS'}


def outcome_clause(claim):
    """Locate the measured endpoint before interpreting a negative sentence.

    Periods in figure labels, decimals and organism abbreviations are not clause
    boundaries. A contrast can describe a negative control and a positive arm
    in the same quote; never apply the first arm's polarity to the second.
    """
    quote = claim.evidence.sentence
    entity = claim.subject if claim.predicate == 'DIFFERS_BETWEEN' else claim.object
    names = [entity.mention, entity.text]
    clauses = []
    for part in re.split(r';|\b(?:although|but|whereas|while|however)\b', quote, flags=re.I):
        start = 0
        for boundary in re.finditer(r'(?<=[.!?])\s+(?=[A-Z])', part):
            word = re.search(r'([A-Za-z]+)[.!?]$', part[:boundary.start()])
            if word and (len(word[1]) == 1 or word[1].casefold() in {'fig','figs','spp','sp','al','vs','dr'}):
                continue
            clauses.append(part[start:boundary.start()])
            start = boundary.end()
        clauses.append(part[start:])
    matched = [c for c in clauses if any(name and source_contains(c, name) for name in names)]
    return matched[0] if len(matched) == 1 else None


def result_view(value):
    """Read both legacy negation and process encodings; missing direction is unknown."""
    raw = value.get('claim_json') or value
    q = value.get('qualifiers') or raw.get('qualifiers') or {}
    processes = list(q.get('processes') or [])
    for role in ('subject', 'object'):
        if (raw.get(role) or {}).get('process'):
            processes.append({'role': role, **raw[role]['process']})
    status = raw.get('result_status', q.get('result_status', 'unknown'))
    if status == 'unknown':
        if any(p.get('direction') == 'unchanged' for p in processes) or q.get('direction') == 'unchanged' or q.get('object_direction') == 'unchanged':
            status = 'no_effect_reported'
        elif value.get('negated', raw.get('negated', False)) and raw.get('predicate', value.get('predicate')) == 'AFFECTS' and not (
                any(p.get('direction') in {'increased','decreased'} for p in processes)
                or q.get('direction') in {'increased','decreased'} or q.get('object_direction') in {'increased','decreased'}):
            status = 'no_effect_reported'
        elif not value.get('negated', raw.get('negated', False)) and (any(p.get('direction') in {'increased', 'decreased'} for p in processes) or q.get('direction') in {'increased', 'decreased'} or q.get('object_direction') in {'increased', 'decreased'}):
            status = 'observed_change'
    return status


def normalize_result(claim, repairs):
    before = claim.model_dump(mode='json')
    status = result_view(before)
    quote = claim.evidence.sentence
    clause = outcome_clause(claim)
    outcome = claim.predicate in {'AFFECTS','DIFFERS_BETWEEN'}
    if outcome and clause and NOT_SIGNIFICANT.search(clause):
        status = 'not_significant'
    if outcome and clause and NO_EFFECT.search(clause) and not NOT_SIGNIFICANT.search(clause):
        status = 'no_effect_reported'
    claim.result_status = status
    if status == 'no_effect_reported' and claim.predicate in {'AFFECTS', 'DIFFERS_BETWEEN'}:
        claim.negated = False
        entity = claim.subject if claim.predicate == 'DIFFERS_BETWEEN' else claim.object
        if entity.process:
            entity.process.direction = 'unchanged'
        else:
            claim.qualifiers['object_direction'] = 'unchanged'
        if claim.predicate == 'DIFFERS_BETWEEN':
            claim.qualifiers['direction'] = 'unchanged'
    if status == 'not_significant':
        # Absence of significance is not proof of zero effect. Keep any reported
        # point-estimate direction, but remove the redundant logical negation.
        if claim.predicate in {'AFFECTS', 'DIFFERS_BETWEEN'}:
            claim.negated = False
            entity = claim.subject if claim.predicate == 'DIFFERS_BETWEEN' else claim.object
            if entity.process and entity.process.direction == 'unchanged' and not NO_EFFECT.search(clause or quote):
                entity.process.direction = 'unspecified'
            for key in ('direction','object_direction'):
                if claim.qualifiers.get(key) == 'unchanged' and not NO_EFFECT.search(clause or quote):
                    claim.qualifiers[key] = 'unspecified'
    if before != claim.model_dump(mode='json'):
        repairs.append({'field': 'result_semantics', 'original': {
            'negated': before['negated'], 'result_status': before['result_status']},
            'replacement': {'negated': claim.negated, 'result_status': status}, 'basis': quote})


def display_negated(value):
    raw = value.get('claim_json') or value
    negated = value.get('negated', raw.get('negated', False))
    predicate = value.get('predicate', raw.get('predicate'))
    return bool(negated and not (predicate in {'AFFECTS','DIFFERS_BETWEEN'}
        and result_view(value) in {'no_effect_reported','not_significant'}))


def check_semantics(claim, repairs):
    """Reject structural errors; quarantine semantic risks as reviewable claims."""
    s, o = claim.subject, claim.object
    quote = ' '.join(e.sentence for e in [claim.evidence, *claim.additional_evidence])
    same = s.text.casefold() == o.text.casefold() and s.type == o.type
    if claim.predicate == 'MEASURED_BY' and (same or s.type == 'Assay'):
        raise ValueError('MEASURED_BY requires a measured entity -> assay, not an assay measuring itself')
    if claim.predicate == 'SUBCLASS_OF' and s.type == 'StudyGroup':
        raise ValueError('Recruitment/group provenance is not SUBCLASS_OF')
    if claim.predicate == 'TARGETS' and s.type in {'Protein', 'Gene'} and o.type in {'Drug', 'Intervention', 'TherapeuticIntervention'}:
        raise ValueError('TARGETS direction must be intervention/antibody -> molecular target')

    risks = [reason for reason in claim.review_reasons if reason.startswith('semantic:')]
    if any(e.text.strip().casefold() in {'unclassified', 'none'} for e in (s,o)):
        risks.append('Adjective or table response label lacks a biological entity identity')
    if claim.predicate == 'TARGETS' and s.type in {'Protein','Gene'} and re.search(r'\bantibod\w*\b', quote, re.I):
        if re.search(r'\b(?:targeting|block|specific|therapy)\b', quote, re.I):
            risks.append('Antibody context does not establish the target protein as the targeting intervention')
    if claim.predicate == 'AFFECTS' and o.type == 'Protein' and not o.process and re.search(r'\btherapeutic activity|\befficacy\b', quote, re.I):
        risks.append('Therapy efficacy is not the activity of the antibody target protein')
    if claim.predicate in {'TREATS', 'PREVENTS'} and s.type in {'Protein', 'Gene'}:
        risks.append('Target protein is not established as the administered intervention; verify antibody role')
    if same and claim.predicate in CAUSAL and not re.search(r'\b(?:autoregulat\w*|self[- ]regulat\w*|autoactivat\w*|autoinhibit\w*)\b', quote, re.I):
        risks.append('Self regulation lacks explicit evidence; preserve the actual intervention as subject')
    if claim.predicate in CAUSAL:
        temporal = re.search(r'\b(?:after|following|subsequent to|subsequently|underwent|suffered)\b', quote, re.I)
        explicit = re.search(r'\b(?:caus\w*|induc\w*|led to|resulted in|reduced|increased|decreased|improved|inhibited|activated|did not affect)\b', quote, re.I)
        if temporal and not explicit and claim.result_status not in {'no_effect_reported','not_significant'}:
            risks.append('Temporal succession alone does not establish treatment causality')
        if re.search(r'\b(?:correlat\w*|associated|co[- ]occurr\w*|predicted potential|functional potential)\b', quote, re.I):
            risks.append('Association or predicted function does not establish a causal mechanism')
        # Inspect only the quoted assertion, not unrelated chunk co-occurrences.
        explicit_joint = re.search(r'\b(?:combined with|in combination with|plus|together with|co-administer\w*|cotreated|co-treated)\b', quote, re.I)
        # Domain cues only request review, never rewrite an intervention. They
        # also cover the coordinated FMT and anti-PD-1 wording in the report.
        fmt = r'(?:FMT|fecal microbiota transplant(?:ation)?)'
        antibody = r'(?:anti[\s–−-]*(?:PD[\s–−-]*[1L]+|programmed)|pembrolizumab)'
        coordinated = re.search(rf'\b{fmt}\s+(?:and|with|plus)\s+{antibody}|\b{antibody}.{{0,30}}\s+and\s+{fmt}\b', quote, re.I)
        if explicit_joint or coordinated:
            interventions = [s, *[p.entity for p in claim.participants if p.role in {'intervention', 'co_intervention', 'treatment'}]]
            if len(interventions) < 2 and not re.search(r'\b(?:combined|combination|plus)\b|\+', s.text, re.I):
                if not (coordinated and re.search(rf'{fmt}.*{antibody}|{antibody}.*{fmt}', s.text, re.I)):
                    risks.append('Joint intervention lacks co_intervention participant; do not infer monotherapy efficacy')
        if s.type == 'StudyGroup' and re.search(r'\b(?:respond\w*|refractory|excluded|donors?|recipients?|patients?)\b', quote, re.I):
            risks.append('Patient/donor/recipient group is not itself the administered causal intervention')
    if not claim.speculative and re.search(r'\b(?:we propose|may|could|support the idea|hypothesi\w*)\b', quote, re.I):
        risks.append('Possible author hypothesis requires speculative/source attribution review')
    if claim.predicate == 'DIFFERS_BETWEEN' and not re.search(r'\b(?:compar\w*|relative to|versus|vs|between|than|higher|lower|similar|unchanged|differ\w*)\b', quote, re.I):
        risks.append('Group descriptions alone do not establish a measured between-group comparison')
    if claim.predicate in {'ASSOCIATED_WITH','CORRELATED_WITH'} and not re.search(r'\b(?:associat\w*|correlat\w*|related|relationship\w*|link\w*)\b', quote, re.I):
        if re.search(r'\b(?:samples?|patients?|assessed|sequencing|abundance|propose|favorable|taxa)\b', quote, re.I):
            risks.append('Sampling context or composition alone does not establish a biological/statistical association')
    if claim.predicate == 'CORRELATED_WITH' and re.search(r'\b(?:belonging to|classified as|annotated as|family of|e\.g\.)', quote, re.I):
        risks.append('Taxonomic assignment or membership must not replace a measured correlation variable')
    if claim.predicate in {'AFFECTS','DIFFERS_BETWEEN'} and (NO_EFFECT.search(quote) or NOT_SIGNIFICANT.search(quote)) and outcome_clause(claim) is None:
        risks.append('Negative result belongs to an unresolved clause; verify endpoint and arm before assigning polarity')
    # Endpoint grounding already checks names. Key qualitative conditions need
    # the same quote support, rather than a different sentence elsewhere in PDF.
    for key in ('comparison_group', 'reference_group', 'time', 'diet', 'dose'):
        value = claim.qualifiers.get(key)
        if isinstance(value, (str, int, float)) and not source_contains(quote, str(value)):
            risks.append(f'Evidence does not cover qualifier {key}')
    if re.search(r'\b(?:analyses|analysis|results)\s+(?:established|showed|demonstrated|suggested)(?:\s+that)?[.! ]*$', claim.evidence.sentence, re.I):
        risks.append('Evidence fragment does not state the relationship')
    for reason in risks:
        if reason not in claim.review_reasons:
            claim.review_reasons.append(reason)
    claim.semantic_status = 'pending' if risks else 'accepted'


def result_sql(alias):
    """Same conservative legacy reading for list and graph filters."""
    return f"""CASE
      WHEN {alias}.claim_json->>'result_status' IN ('not_significant','no_effect_reported','observed_change')
        THEN {alias}.claim_json->>'result_status'
      WHEN {alias}.claim_json->'qualifiers'->>'result_status' IN ('not_significant','no_effect_reported','observed_change')
        THEN {alias}.claim_json->'qualifiers'->>'result_status'
      WHEN {alias}.claim_json->'subject'->'process'->>'direction'='unchanged'
        OR {alias}.claim_json->'object'->'process'->>'direction'='unchanged'
        OR {alias}.claim_json->'qualifiers'->>'direction'='unchanged'
        OR {alias}.claim_json->'qualifiers'->>'object_direction'='unchanged'
        OR {alias}.claim_json @> '{{"qualifiers":{{"processes":[{{"direction":"unchanged"}}]}}}}'::jsonb
        OR ({alias}.negated AND {alias}.predicate='AFFECTS'
            AND COALESCE({alias}.claim_json->'subject'->'process'->>'direction','') NOT IN ('increased','decreased')
            AND COALESCE({alias}.claim_json->'object'->'process'->>'direction','') NOT IN ('increased','decreased')
            AND COALESCE({alias}.claim_json->'qualifiers'->>'direction','') NOT IN ('increased','decreased')
            AND COALESCE({alias}.claim_json->'qualifiers'->>'object_direction','') NOT IN ('increased','decreased')
            AND NOT {alias}.claim_json @> '{{"qualifiers":{{"processes":[{{"direction":"increased"}}]}}}}'::jsonb
            AND NOT {alias}.claim_json @> '{{"qualifiers":{{"processes":[{{"direction":"decreased"}}]}}}}'::jsonb) THEN 'no_effect_reported'
      WHEN NOT {alias}.negated AND ({alias}.claim_json->'subject'->'process'->>'direction' IN ('increased','decreased')
        OR {alias}.claim_json->'object'->'process'->>'direction' IN ('increased','decreased')
        OR {alias}.claim_json->'qualifiers'->>'direction' IN ('increased','decreased')
        OR {alias}.claim_json->'qualifiers'->>'object_direction' IN ('increased','decreased')
        OR {alias}.claim_json @> '{{"qualifiers":{{"processes":[{{"direction":"increased"}}]}}}}'::jsonb
        OR {alias}.claim_json @> '{{"qualifiers":{{"processes":[{{"direction":"decreased"}}]}}}}'::jsonb) THEN 'observed_change'
      ELSE 'unknown' END"""
