"""Complete assertion projection, alongside the compatible binary graph."""
import re
from psycopg.types.json import Jsonb

from app.catalog import PROCESSING_VERSION, add_review, digest
from app.normalization import canonicalize_qualifiers
from app.schemas import Entity, Participant


def normalized_participants(claim):
    """Project explicit named qualifier roles, including legacy claim records."""
    values = list(claim.participants)
    explicit_roles = {p.role for p in values}
    for role, entity_type in {'cell_line':'CellLine','species':'Organism','assay':'Assay','tissue':'Tissue','cell_type':'CellType'}.items():
        value = claim.qualifiers.get(role)
        if role not in explicit_roles and isinstance(value, str) and value.strip():
            values.append(Participant(role=role,entity=Entity(text=value,type=entity_type)))
    return values


def fact_qualifiers(claim):
    qualifiers = canonicalize_qualifiers(claim.qualifiers)
    processes = []
    for role, entity in [("subject", claim.subject), ("object", claim.object), *[(p.role, p.entity) for p in claim.participants]]:
        if entity.process:
            # Original wording is retained in claim_json and participant.mention;
            # it must not split otherwise identical relation identities.
            processes.append({"role": role, **entity.process.model_dump()})
    if processes:
        qualifiers["processes"] = processes
    return qualifiers


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
        if not entity.process and re.search(r"\b(expression|secretion|activity)\b", entity.text, re.I):
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
