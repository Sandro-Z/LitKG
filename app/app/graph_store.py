from typing import Any

from psycopg.types.json import Jsonb

from app.normalization import (
    EntityIdentity,
    canonicalize_qualifiers,
    claim_fingerprint,
    clean_text,
    entity_identity,
    normalize_predicate,
    qualifiers_fingerprint,
)
from app.schemas import Claim


def upsert_entity(cur, identity: EntityIdentity) -> int:
    # Serialize identities that share an alias or external ID. This avoids
    # producing a name-only node and an ID-backed node when concurrent papers
    # introduce the same entity in different forms.
    lock_keys = {
        (
            f"entity-alias|{identity.entity_type.casefold()}|"
            f"{identity.normalized_alias}"
        )
    }
    if identity.normalized_id:
        lock_keys.add(f"entity-id|{identity.canonical_key}")
    for lock_key in sorted(lock_keys):
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )

    cur.execute(
        """
        SELECT id, normalized_id
        FROM entities
        WHERE canonical_key = %s
        """,
        (identity.canonical_key,),
    )
    existing = cur.fetchone()

    if not existing:
        cur.execute(
            """
            SELECT DISTINCT entity.id, entity.normalized_id
            FROM entities entity
            JOIN entity_aliases alias ON alias.entity_id = entity.id
            WHERE entity.entity_type = %s
              AND alias.normalized_alias = %s
            ORDER BY entity.id
            LIMIT 2
            """,
            (identity.entity_type, identity.normalized_alias),
        )
        alias_candidates = cur.fetchall()

        reusable = None
        if len(alias_candidates) == 1:
            candidate = alias_candidates[0]
            if not identity.normalized_id:
                reusable = candidate
            elif (
                candidate["normalized_id"] is None
                or candidate["normalized_id"].casefold()
                == identity.normalized_id.casefold()
            ):
                reusable = candidate

        if reusable:
            entity_id = reusable["id"]
            if identity.normalized_id and not reusable["normalized_id"]:
                cur.execute(
                    """
                    UPDATE entities
                    SET
                        canonical_key = %s,
                        normalized_id = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        identity.canonical_key,
                        identity.normalized_id,
                        entity_id,
                    ),
                )
        else:
            cur.execute(
                """
                INSERT INTO entities (
                    canonical_key,
                    canonical_name,
                    entity_type,
                    normalized_id
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (canonical_key)
                DO UPDATE SET
                    normalized_id = COALESCE(
                        entities.normalized_id,
                        EXCLUDED.normalized_id
                    ),
                    updated_at = now()
                RETURNING id
                """,
                (
                    identity.canonical_key,
                    identity.canonical_name,
                    identity.entity_type,
                    identity.normalized_id,
                ),
            )
            entity_id = cur.fetchone()["id"]
    else:
        entity_id = existing["id"]
        if identity.normalized_id and not existing["normalized_id"]:
            cur.execute(
                """
                UPDATE entities
                SET normalized_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (identity.normalized_id, entity_id),
            )

    cur.execute(
        """
        INSERT INTO entity_aliases (entity_id, alias, normalized_alias)
        VALUES (%s, %s, %s)
        ON CONFLICT (entity_id, normalized_alias)
        DO UPDATE SET alias = EXCLUDED.alias
        """,
        (
            entity_id,
            identity.canonical_name,
            identity.normalized_alias,
        ),
    )
    return entity_id


def _claim_projection(cur, claim: Claim) -> dict[str, Any]:
    subject = entity_identity(
        claim.subject.text,
        claim.subject.type,
        claim.subject.normalized_id,
    )
    object_ = entity_identity(
        claim.object.text,
        claim.object.type,
        claim.object.normalized_id,
    )
    subject_entity_id = upsert_entity(cur, subject)
    object_entity_id = upsert_entity(cur, object_)
    predicate = normalize_predicate(claim.predicate)
    qualifiers = canonicalize_qualifiers(claim.qualifiers)
    evidence_text = clean_text(claim.evidence.sentence)

    return {
        "subject": subject,
        "object": object_,
        "subject_entity_id": subject_entity_id,
        "object_entity_id": object_entity_id,
        "predicate": predicate,
        "qualifiers": qualifiers,
        "qualifiers_hash": qualifiers_fingerprint(qualifiers),
        "evidence_text": evidence_text,
        "fingerprint": claim_fingerprint(
            subject=subject,
            predicate=predicate,
            object_=object_,
            qualifiers=qualifiers,
            evidence_text=evidence_text,
            negated=claim.negated,
            speculative=claim.speculative,
        ),
    }


def _upsert_relation_evidence(
    cur,
    *,
    claim_id: int,
    projection: dict[str, Any],
    negated: bool,
    speculative: bool,
) -> int:
    cur.execute(
        """
        INSERT INTO relations (
            subject_entity_id,
            predicate,
            object_entity_id,
            qualifiers,
            qualifiers_hash,
            negated,
            speculative
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (
            subject_entity_id,
            predicate,
            object_entity_id,
            qualifiers_hash,
            negated,
            speculative
        )
        DO UPDATE SET
            qualifiers = EXCLUDED.qualifiers,
            updated_at = now()
        RETURNING id
        """,
        (
            projection["subject_entity_id"],
            projection["predicate"],
            projection["object_entity_id"],
            Jsonb(projection["qualifiers"]),
            projection["qualifiers_hash"],
            negated,
            speculative,
        ),
    )
    relation_id = cur.fetchone()["id"]

    cur.execute(
        """
        INSERT INTO relation_evidence (relation_id, claim_id)
        VALUES (%s, %s)
        ON CONFLICT (claim_id)
        DO UPDATE SET relation_id = EXCLUDED.relation_id
        """,
        (relation_id, claim_id),
    )
    return relation_id


def store_claim(
    cur,
    *,
    document_id: int,
    chunk_id: int,
    claim: Claim,
    extraction_model: str,
) -> tuple[int, int]:
    projection = _claim_projection(cur, claim)
    claim_json = claim.model_dump(mode="json")

    cur.execute(
        """
        INSERT INTO claims (
            document_id,
            chunk_id,
            subject_text,
            subject_type,
            subject_normalized_id,
            subject_entity_id,
            predicate,
            object_text,
            object_type,
            object_normalized_id,
            object_entity_id,
            evidence_text,
            confidence,
            negated,
            speculative,
            claim_fingerprint,
            claim_json,
            extraction_model
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (chunk_id, claim_fingerprint)
        DO UPDATE SET
            subject_text = EXCLUDED.subject_text,
            subject_type = EXCLUDED.subject_type,
            subject_normalized_id = EXCLUDED.subject_normalized_id,
            subject_entity_id = EXCLUDED.subject_entity_id,
            predicate = EXCLUDED.predicate,
            object_text = EXCLUDED.object_text,
            object_type = EXCLUDED.object_type,
            object_normalized_id = EXCLUDED.object_normalized_id,
            object_entity_id = EXCLUDED.object_entity_id,
            evidence_text = EXCLUDED.evidence_text,
            confidence = EXCLUDED.confidence,
            negated = EXCLUDED.negated,
            speculative = EXCLUDED.speculative,
            claim_json = EXCLUDED.claim_json,
            extraction_model = EXCLUDED.extraction_model
        RETURNING id
        """,
        (
            document_id,
            chunk_id,
            projection["subject"].canonical_name,
            projection["subject"].entity_type,
            projection["subject"].normalized_id,
            projection["subject_entity_id"],
            projection["predicate"],
            projection["object"].canonical_name,
            projection["object"].entity_type,
            projection["object"].normalized_id,
            projection["object_entity_id"],
            projection["evidence_text"],
            claim.confidence,
            claim.negated,
            claim.speculative,
            projection["fingerprint"],
            Jsonb(claim_json),
            extraction_model,
        ),
    )
    claim_id = cur.fetchone()["id"]
    relation_id = _upsert_relation_evidence(
        cur,
        claim_id=claim_id,
        projection=projection,
        negated=claim.negated,
        speculative=claim.speculative,
    )
    return claim_id, relation_id


def project_existing_claim(cur, claim_id: int, claim_json: dict[str, Any]) -> int:
    claim = Claim.model_validate(claim_json)
    projection = _claim_projection(cur, claim)

    # Old versions could store the same extraction more than once. When
    # backfilling, keep the first matching row so the new unique fingerprint
    # index does not turn legacy duplication into a failed migration.
    cur.execute(
        """
        SELECT duplicate.id
        FROM claims current
        JOIN claims duplicate
          ON duplicate.chunk_id = current.chunk_id
         AND duplicate.claim_fingerprint = %s
         AND duplicate.id <> current.id
        WHERE current.id = %s
        ORDER BY duplicate.id
        LIMIT 1
        """,
        (projection["fingerprint"], claim_id),
    )
    duplicate = cur.fetchone()
    if duplicate:
        cur.execute("DELETE FROM claims WHERE id = %s", (claim_id,))
        claim_id = duplicate["id"]

    cur.execute(
        """
        UPDATE claims
        SET
            subject_text = %s,
            subject_type = %s,
            subject_normalized_id = %s,
            subject_entity_id = %s,
            predicate = %s,
            object_text = %s,
            object_type = %s,
            object_normalized_id = %s,
            object_entity_id = %s,
            evidence_text = %s,
            claim_fingerprint = %s
        WHERE id = %s
        """,
        (
            projection["subject"].canonical_name,
            projection["subject"].entity_type,
            projection["subject"].normalized_id,
            projection["subject_entity_id"],
            projection["predicate"],
            projection["object"].canonical_name,
            projection["object"].entity_type,
            projection["object"].normalized_id,
            projection["object_entity_id"],
            projection["evidence_text"],
            projection["fingerprint"],
            claim_id,
        ),
    )
    return _upsert_relation_evidence(
        cur,
        claim_id=claim_id,
        projection=projection,
        negated=claim.negated,
        speculative=claim.speculative,
    )


def prune_orphan_projection(cur) -> dict[str, int]:
    cur.execute(
        """
        DELETE FROM relations r
        WHERE NOT EXISTS (
            SELECT 1
            FROM relation_evidence re
            WHERE re.relation_id = r.id
        )
        """
    )
    deleted_relations = cur.rowcount

    cur.execute(
        """
        DELETE FROM entities e
        WHERE NOT EXISTS (
            SELECT 1
            FROM relations r
            WHERE r.subject_entity_id = e.id OR r.object_entity_id = e.id
        )
        """
    )
    deleted_entities = cur.rowcount
    return {
        "deleted_relations": deleted_relations,
        "deleted_entities": deleted_entities,
    }
