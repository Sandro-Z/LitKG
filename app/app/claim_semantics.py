"""Conservative result semantics and candidate-local checks, without model calls.

Rules flag risks; they do not prove entailment or manufacture a new relation.
Original wording is always retained in evidence and repair history.
"""
import re

from app.catalog import source_contains


NOT_SIGNIFICANT = re.compile(r'\b(?:not statistically significant|no significant (?:difference|change|effect)|did not significantly|not significantly (?:different|alter|affect|change))\b', re.I)
NO_EFFECT = re.compile(r'\b(?:did not (?:affect|alter|change)|had no effect on|failed to (?:affect|influence)|unchanged|remained (?:stable|constant)|no (?:observed )?(?:difference|change|effect)|(?:was|were) not (?:affected|altered|changed))\b', re.I)
CAUSAL = {'CAUSES', 'TREATS', 'PREVENTS', 'AFFECTS', 'ACTIVATES', 'INHIBITS'}
OUTCOMES = {'AFFECTS', 'ACTIVATES', 'INHIBITS', 'DIFFERS_BETWEEN'}
NOT_ASSESSED = re.compile(r'\b(?:not (?:tested|assessed|measured|examined|evaluated)|did not (?:test|assess|measure|examine|evaluate))\b', re.I)
NOT_DETECTED = re.compile(r'\b(?:not detected|undetectable|below (?:the )?(?:limit of detection|detection limit))\b', re.I)
NON_OUTCOMES = {'not_assessed', 'not_detected', 'absence_reported', 'unresolved_negative'}
PROCEDURAL = re.compile(r'\b(?:DNA extraction|RNA extraction|liquid handling|pipetting|centrifugation|genome assembly|gene annotation|functional annotation|read mapping|data plotting|statistical analysis)\b', re.I)


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
    """Read endpoint-specific outcome metadata; logical negation is not a null effect."""
    raw = value.get('claim_json') or value
    q = value.get('qualifiers') or raw.get('qualifiers') or {}
    predicate = value.get('predicate', raw.get('predicate'))
    if predicate not in OUTCOMES or q.get('result_interpretation') in NON_OUTCOMES:
        return 'unknown'
    for status in (raw.get('result_status'), q.get('result_status')):
        if status in {'no_effect_reported', 'not_significant', 'observed_change'}:
            return status
    if value.get('negated', raw.get('negated', False)):
        return 'unknown'
    role = 'subject' if predicate == 'DIFFERS_BETWEEN' else 'object'
    directions = [((raw.get(role) or {}).get('process') or {}).get('direction')]
    directions += [p.get('direction') for p in (q.get('processes') or []) if isinstance(p, dict) and p.get('role') == role]
    directions.append(q.get('direction' if role == 'subject' else 'object_direction'))
    changed = any(d in {'increased', 'decreased'} for d in directions)
    if changed and 'unchanged' not in directions:
        return 'observed_change'
    if 'unchanged' in directions and not changed:
        return 'no_effect_reported'
    return 'unknown'


def normalize_result(claim, repairs):
    before = claim.model_dump(mode='json')
    status = result_view(before)
    quote = claim.evidence.sentence
    clause = outcome_clause(claim)
    outcome = claim.predicate in {'AFFECTS','DIFFERS_BETWEEN'}
    unavailable = clause and (NOT_ASSESSED.search(clause) or NOT_DETECTED.search(clause))
    if outcome and unavailable:
        status = 'unknown'
        claim.qualifiers['result_interpretation'] = 'not_assessed' if NOT_ASSESSED.search(clause) else 'not_detected'
        reason = 'semantic: lack of measurement/detection must not be interpreted as a measured null effect'
        if reason not in claim.review_reasons:
            claim.review_reasons.append(reason)
    elif outcome and clause and NOT_SIGNIFICANT.search(clause):
        status = 'not_significant'
    elif outcome and clause and NO_EFFECT.search(clause):
        status = 'no_effect_reported'
    elif outcome and status == 'no_effect_reported':
        status = 'unknown'
        claim.qualifiers['result_interpretation'] = 'unresolved_negative'
        reason = 'semantic: measured null effect is not explicit in the unique endpoint clause'
        if reason not in claim.review_reasons:
            claim.review_reasons.append(reason)
    if claim.predicate not in OUTCOMES and before['result_status'] != 'unknown':
        claim.review_reasons.append('semantic: result_status requires a measured outcome; relation negation remains separate')
    claim.result_status = status
    if status == 'no_effect_reported' and claim.predicate in {'AFFECTS', 'DIFFERS_BETWEEN'}:
        claim.negated = False
        entity = claim.subject if claim.predicate == 'DIFFERS_BETWEEN' else claim.object
        if entity.process:
            entity.process.direction = 'unchanged'
        else:
            key = 'direction' if claim.predicate == 'DIFFERS_BETWEEN' else 'object_direction'
            claim.qualifiers[key] = 'unchanged'
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

    risks = [reason for reason in claim.review_reasons if reason.startswith(('semantic:', 'predicate registry:'))]
    if claim.source_kind == 'unknown':
        risks.append('semantic: source attribution is unknown; verify primary result versus cited/background statement')
    if any(e.type in {'BiologicalProcess', 'MicrobialFunction'} and PROCEDURAL.search(e.text) for e in (s, o)):
        risks.append('semantic: laboratory/computational procedure is typed as a biological process')
    if claim.predicate == 'MEASURED_BY' and re.search(r'\b(?:extract\w*|assembl\w*|annotat\w*|plott\w*|pipett\w*)\b', quote, re.I):
        if not re.search(r'\b(?:measur\w*|quantif\w*|detect\w*|assay\w*)\b', quote, re.I):
            risks.append('semantic: sample preparation/annotation is not an established measurement relationship')
    if claim.predicate in OUTCOMES and s.process and o.type in {'Intervention', 'TherapeuticIntervention', 'Drug', 'StudyGroup'}:
        if claim.predicate != 'DIFFERS_BETWEEN':
            risks.append('semantic: measured outcome appears as subject and treatment/group as object; verify event roles')
    if o.process and o.process.kind == 'expression' and o.type in {'StudyGroup', 'Phenotype', 'Disease', 'Intervention', 'Drug'}:
        risks.append('semantic: expression must be anchored to the expressed gene/RNA/protein, with group/phenotype as context')
    if re.search(r'\brespectively\b', quote, re.I):
        risks.append('semantic: coordinated respectively mapping requires pair-by-pair source review')
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
    if claim.predicate == 'DIFFERS_BETWEEN' and re.search(r'\b(?:accuracy|AUC|ROC|precision|recall|F1|prediction error)\b', str(claim.qualifiers.get('metric','')), re.I):
        if re.search(r'\b(?:classifier\w*|training (?:labels|data)|prediction models?|predictors?)\b', quote, re.I):
            risks.append('semantic: model/label/performance comparison is not a measured contrast between StudyGroup participants')
    if claim.predicate in {'ASSOCIATED_WITH','CORRELATED_WITH'} and not re.search(r'\b(?:associat\w*|correlat\w*|related|relationship\w*|link\w*)\b', quote, re.I):
        if re.search(r'\b(?:samples?|patients?|assessed|sequencing|abundance|propose|favorable|taxa)\b', quote, re.I):
            risks.append('Sampling context or composition alone does not establish a biological/statistical association')
    if claim.predicate == 'CORRELATED_WITH' and re.search(r'\b(?:belonging to|classified as|annotated as|family of|e\.g\.)', quote, re.I):
        risks.append('Taxonomic assignment or membership must not replace a measured correlation variable')
    if claim.predicate in {'AFFECTS','DIFFERS_BETWEEN'} and (NO_EFFECT.search(quote) or NOT_SIGNIFICANT.search(quote)) and outcome_clause(claim) is None:
        risks.append('Negative result belongs to an unresolved clause; verify endpoint and arm before assigning polarity')
    # Endpoint grounding already checks names. Key qualitative conditions need
    # the same quote support, rather than a different sentence elsewhere in PDF.
    for key in ('comparison_group', 'reference_group', 'metric', 'time', 'diet', 'dose'):
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
    role = f"CASE WHEN {alias}.predicate='DIFFERS_BETWEEN' THEN 'subject' ELSE 'object' END"
    direction = f"CASE WHEN {alias}.predicate='DIFFERS_BETWEEN' THEN 'direction' ELSE 'object_direction' END"
    def has(values):
        quoted = ','.join("'" + v + "'" for v in values)
        return f"""({alias}.claim_json->({role})->'process'->>'direction' IN ({quoted})
          OR {alias}.claim_json->'qualifiers'->>({direction}) IN ({quoted})
          OR EXISTS (SELECT 1 FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof({alias}.claim_json->'qualifiers'->'processes')='array'
              THEN {alias}.claim_json->'qualifiers'->'processes' ELSE '[]'::jsonb END) p
            WHERE p->>'role'=({role}) AND p->>'direction' IN ({quoted})))"""
    changed, unchanged = has(('increased','decreased')), has(('unchanged',))
    return f"""CASE
      WHEN {alias}.predicate NOT IN ('AFFECTS','ACTIVATES','INHIBITS','DIFFERS_BETWEEN')
        OR {alias}.claim_json->'qualifiers'->>'result_interpretation' IN ('not_assessed','not_detected','absence_reported','unresolved_negative') THEN 'unknown'
      WHEN {alias}.claim_json->>'result_status' IN ('not_significant','no_effect_reported','observed_change')
        THEN {alias}.claim_json->>'result_status'
      WHEN {alias}.claim_json->'qualifiers'->>'result_status' IN ('not_significant','no_effect_reported','observed_change')
        THEN {alias}.claim_json->'qualifiers'->>'result_status'
      WHEN {alias}.negated THEN 'unknown'
      WHEN COALESCE({unchanged},false) AND NOT COALESCE({changed},false) THEN 'no_effect_reported'
      WHEN COALESCE({changed},false) AND NOT COALESCE({unchanged},false) THEN 'observed_change'
      ELSE 'unknown' END"""
