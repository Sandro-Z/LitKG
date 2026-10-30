"""Complete assertion projection, alongside the compatible binary graph."""
from psycopg.types.json import Jsonb

from app.catalog import PROCESSING_VERSION, add_review, digest
from app.normalization import canonicalize_qualifiers, entity_identity, normalize_predicate, stable_json
from app.schemas import Entity, Participant
from app.processes import PROCESS_DEFINITIONS, PROCESS_PATTERN


def canonical_comparison_qualifiers(predicate, qualifiers):
    """Give two explicit encodings of the same measured comparison one identity.

    Only exact process-kind metrics qualify. Do not guess that e.g. frequency,
    percentage or a named diversity index is equivalent to another measurement.
    Original model output stays in claim_json.
    """
    result = canonicalize_qualifiers(qualifiers)
    if normalize_predicate(predicate) != 'DIFFERS_BETWEEN':
        return result
    metric, direction = result.get('metric'), result.get('direction')
    processes = result.get('processes', [])
    if (isinstance(metric, str) and metric in PROCESS_DEFINITIONS
            and isinstance(direction, str)
            and direction in {'increased', 'decreased', 'unchanged', 'unspecified'}
            and not any(p.get('role') == 'subject' for p in processes)):
        result['processes'] = sorted([
            *processes, {'role': 'subject', 'kind': metric, 'direction': direction}
        ], key=stable_json)
    return result


def normalized_participants(claim):
    """Project explicit named qualifier roles, including legacy claim records."""
    values = list(claim.participants)
    explicit_roles = {p.role for p in values}
    defaults = {'cell_line':'CellLine','species':'Organism','assay':'Assay','tissue':'Tissue','cell_type':'CellType',
                'comparison_group':'StudyGroup','reference_group':'StudyGroup'}
    for role in (*defaults, 'substrate', 'product', 'mediator', 'intervention', 'co_intervention', 'host'):
        value = claim.qualifiers.get(role)
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict):
                try:
                    entity = Entity.model_validate(candidate)
                    if not entity.text.strip() or not entity.type or entity.type == 'Other':
                        raise ValueError('Named qualifier role needs an explicit entity type')
                except ValueError:
                    reason = f'semantic: invalid typed qualifier participant {role}'
                    if reason not in claim.review_reasons:
                        claim.review_reasons.append(reason)
                    continue
            elif role in defaults and role not in explicit_roles and isinstance(candidate, str) and candidate.strip():
                entity = Entity(text=candidate, type=defaults[role])
            else:
                continue  # Bare product/mediator names do not establish a type.
            key = (role, entity.type, (entity.mention or entity.text).strip().casefold())
            if not any((p.role, p.entity.type, (p.entity.mention or p.entity.text).strip().casefold()) == key for p in values):
                values.append(Participant(role=role, entity=entity))
    return values


def fact_qualifiers(claim, participants=None):
    qualifiers = canonicalize_qualifiers(claim.qualifiers)
    qualifiers['source_kind'] = claim.source_kind
    if claim.result_status != 'unknown':
        qualifiers['result_status'] = claim.result_status
    processes = []
    values = participants if participants is not None else [
        {"role": role, "entity": entity} for role, entity in
        [("subject", claim.subject), ("object", claim.object), *[(p.role, p.entity) for p in claim.participants]]]
    for participant in values:
        role, entity = participant['role'], participant['entity']
        if entity.process:
            # Original wording is retained in claim_json and participant.mention;
            # it must not split otherwise identical relation identities.
            process = {"role": role, **entity.process.model_dump()}
            if role not in {"subject", "object"}:
                # Repeated roles (e.g. two substrates) must still identify whose
                # production/concentration changed. Graph projection supplies IDs.
                if 'entity_id' in participant:
                    process['entity_id'] = participant['entity_id']
                else:
                    process['entity_key'] = entity_identity(entity.text, entity.type, entity.normalized_id).canonical_key
            processes.append(process)
    if processes:
        qualifiers["processes"] = sorted(processes, key=stable_json)
    return canonical_comparison_qualifiers(claim.predicate, qualifiers)


def store_fact(cur, claim_id, relation_id, claim, projection, document_id):
    cur.execute("""INSERT INTO derived_dirty_documents (document_id) VALUES (%s)
        ON CONFLICT (document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1""", (document_id,))
    cur.execute("SELECT catalog_version FROM documents WHERE id = %s", (document_id,))
    version = cur.fetchone()["catalog_version"]
    participants = projection["participants"]
    key = digest({"participants": sorted((p["role"], p["entity_id"]) for p in participants),
                  "predicate": projection["predicate"], "qualifiers": projection["qualifiers"],
                  "negated": claim.negated, "speculative": claim.speculative})
    cur.execute("""
        INSERT INTO facts (claim_id, relation_id, fact_key, predicate, qualifiers, negated, speculative, catalog_version, processing_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (claim_id) DO UPDATE SET relation_id = EXCLUDED.relation_id, fact_key = EXCLUDED.fact_key,
          predicate = EXCLUDED.predicate, qualifiers = EXCLUDED.qualifiers, negated = EXCLUDED.negated,
          speculative = EXCLUDED.speculative, catalog_version = EXCLUDED.catalog_version,
          processing_version = EXCLUDED.processing_version, updated_at = now() RETURNING id
    """, (claim_id, relation_id, key, projection["predicate"], Jsonb(projection["qualifiers"]), claim.negated, claim.speculative, version, PROCESSING_VERSION))
    fact_id = cur.fetchone()["id"]
    cur.execute("DELETE FROM fact_participants WHERE fact_id = %s", (fact_id,))
    cur.execute("DELETE FROM fact_evidence WHERE fact_id = %s", (fact_id,))
    for participant in participants:
        cur.execute("""INSERT INTO fact_participants (fact_id, entity_id, role, mention) VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING""", (fact_id, participant["entity_id"], participant["role"], participant["mention"]))
    for index, evidence in enumerate([claim.evidence, *claim.additional_evidence]):
        cur.execute("INSERT INTO fact_evidence (fact_id, ordinal, evidence) VALUES (%s, %s, %s)",
                    (fact_id, index, Jsonb(evidence.model_dump(mode="json"))))
    for reason in claim.review_reasons:
        add_review(cur, document_id, version, "semantic_split", {"claim_id": claim_id, "reason": reason})
    for p in participants:
        entity = p["entity"]
        if entity.type not in {'BiologicalProcess', 'MicrobialFunction', 'DiversityMetric', 'Assay', 'Pathway'} and not entity.process and PROCESS_PATTERN.search(entity.text):
            add_review(cur, document_id, version, "semantic_split", {"claim_id": claim_id, "reason": "unsplit process-bearing name", "entity": entity.model_dump()})
        if entity.uncertainty_reason or entity.type == "Other":
            add_review(cur, document_id, version, "uncertain_entity", {"claim_id": claim_id, "entity": entity.model_dump()})
    # Same context, opposite polarity: a candidate conflict, never an automatic
    # resolution. Different conditions are deliberately not conflated.
    cur.execute("""SELECT other.id FROM facts other JOIN claims c ON c.id = other.claim_id
        WHERE c.document_id = %s AND other.id <> %s AND other.predicate = %s
          AND other.qualifiers = %s AND other.negated <> %s
          AND ARRAY(SELECT role || ':' || entity_id FROM fact_participants WHERE fact_id = other.id ORDER BY role, entity_id)
            = ARRAY(SELECT role || ':' || entity_id FROM fact_participants WHERE fact_id = %s ORDER BY role, entity_id)
        LIMIT 20""", (document_id, fact_id, projection["predicate"], Jsonb(projection["qualifiers"]), claim.negated, fact_id))
    for other in cur.fetchall():
        add_review(cur, document_id, version, "semantic_conflict", {"fact_ids": sorted([fact_id, other["id"]])})
    return fact_id
