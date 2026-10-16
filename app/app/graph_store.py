from dataclasses import replace
from typing import Any

from psycopg.types.json import Jsonb

from app.normalization import (
    EntityIdentity,
    canonicalize_qualifiers,
    claim_fingerprint,
    clean_text,
    compatible_entity_types,
    entity_family,
    entity_identity,
    identifier_namespace,
    normalize_alias,
    normalize_entity_alias,
    normalize_external_id,
    normalize_predicate,
    qualifiers_fingerprint,
)
from app.schemas import Claim
from app.facts import fact_qualifiers, normalized_participants, store_fact
from app.catalog import PROCESSING_VERSION, digest, load_catalog, resolve_bundle
from app.schemas import ClaimBundle


def _identity_locks(identity: EntityIdentity) -> set[str]:
    family = entity_family(identity.entity_type)
    keys = {
        f"entity-alias|{family}|{normalize_entity_alias(alias, identity.entity_type)}"
        for alias in identity.aliases or (identity.canonical_name,)
    }
    if identity.normalized_id:
        keys.add(f"entity-id|{family}|{identity.normalized_id.casefold()}")
    return keys


def _candidate_identifiers(cur, candidate) -> list[str]:
    cur.execute("SELECT normalized_id FROM entity_identifiers WHERE entity_id = %s", (candidate["id"],))
    values = [row["normalized_id"] for row in cur.fetchall()]
    if candidate["normalized_id"]:
        values.append(candidate["normalized_id"])
    return [normalize_external_id(value) for value in values]


def _merge_entities(cur, target, sources):
    """Rewire equivalent nodes atomically, preserving every claim and its JSON."""
    source_ids = [row["id"] for row in sources]
    if not source_ids:
        return
    target_id = target["id"]
    cur.execute("""SELECT DISTINCT f.id,c.document_id FROM facts f JOIN claims c ON c.id=f.claim_id
        JOIN fact_participants p ON p.fact_id=f.id WHERE p.entity_id=ANY(%s::BIGINT[])""", (source_ids,))
    affected_facts = cur.fetchall()
    cur.execute("""INSERT INTO derived_dirty_documents (document_id)
        SELECT DISTINCT de.document_id FROM document_entities de WHERE de.entity_id=ANY(%s::BIGINT[])
        UNION SELECT DISTINCT c.document_id FROM claims c JOIN facts f ON f.claim_id=c.id
        JOIN fact_participants p ON p.fact_id=f.id WHERE p.entity_id=ANY(%s::BIGINT[])
        ON CONFLICT (document_id) DO UPDATE SET revision=derived_dirty_documents.revision+1""", (source_ids,source_ids))
    cur.execute("""SELECT * FROM relations WHERE subject_entity_id = ANY(%s::BIGINT[]) OR object_entity_id = ANY(%s::BIGINT[])
        OR EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(qualifiers->'participants','[]'::jsonb)) p
                   WHERE (p->>1)::BIGINT=ANY(%s::BIGINT[])) ORDER BY id FOR UPDATE""", (source_ids, source_ids, source_ids))
    for relation in cur.fetchall():
        subject = target_id if relation["subject_entity_id"] in source_ids else relation["subject_entity_id"]
        object_ = target_id if relation["object_entity_id"] in source_ids else relation["object_entity_id"]
        qualifiers = dict(relation['qualifiers'])
        if 'participants' in qualifiers:
            qualifiers['participants'] = sorted({(role,target_id if entity_id in source_ids else entity_id) for role,entity_id in qualifiers['participants']})
        cur.execute(
            """
            INSERT INTO relations (subject_entity_id, predicate, object_entity_id, qualifiers, qualifiers_hash, negated, speculative)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (subject_entity_id, predicate, object_entity_id, qualifiers_hash, negated, speculative)
            DO UPDATE SET updated_at = now() RETURNING id
            """,
            (subject, relation["predicate"], object_, Jsonb(qualifiers), qualifiers_fingerprint(qualifiers), relation["negated"], relation["speculative"]),
        )
        relation_id = cur.fetchone()["id"]
        cur.execute("UPDATE facts SET relation_id = %s WHERE relation_id = %s", (relation_id, relation["id"]))
        cur.execute("UPDATE relation_evidence SET relation_id = %s WHERE relation_id = %s", (relation_id, relation["id"]))
        if relation_id != relation['id']:
            cur.execute("DELETE FROM relations WHERE id = %s", (relation["id"],))
    cur.execute("UPDATE claims SET subject_entity_id = %s WHERE subject_entity_id = ANY(%s::BIGINT[])", (target_id, source_ids))
    cur.execute("UPDATE claims SET object_entity_id = %s WHERE object_entity_id = ANY(%s::BIGINT[])", (target_id, source_ids))
    # Complete facts and directory entries refer to the same normalized IDs.
    cur.execute("""INSERT INTO fact_participants (fact_id, entity_id, role, mention)
        SELECT fact_id, %s, role, mention FROM fact_participants WHERE entity_id = ANY(%s::BIGINT[])
        ON CONFLICT DO NOTHING""", (target_id, source_ids))
    cur.execute("DELETE FROM fact_participants WHERE entity_id = ANY(%s::BIGINT[])", (source_ids,))
    for fact in affected_facts:
        cur.execute('SELECT * FROM facts WHERE id=%s', (fact['id'],))
        current = cur.fetchone()
        cur.execute('SELECT role,entity_id FROM fact_participants WHERE fact_id=%s ORDER BY role,entity_id', (fact['id'],))
        participants = [(p['role'],p['entity_id']) for p in cur.fetchall()]
        qualifiers = dict(current['qualifiers'])
        if 'participants' in qualifiers:
            qualifiers['participants'] = sorted({(role,target_id if entity_id in source_ids else entity_id) for role,entity_id in qualifiers['participants']})
        key = digest({'participants':participants,'predicate':current['predicate'],'qualifiers':qualifiers,
                      'negated':current['negated'],'speculative':current['speculative']})
        cur.execute('UPDATE facts SET fact_key=%s,qualifiers=%s,updated_at=now() WHERE id=%s', (key,Jsonb(qualifiers),fact['id']))
    cur.execute("UPDATE document_entities SET entity_id = %s WHERE entity_id = ANY(%s::BIGINT[])", (target_id, source_ids))
    cur.execute(
        """
        INSERT INTO entity_aliases (entity_id, alias, normalized_alias)
        SELECT %s, alias, normalized_alias FROM entity_aliases WHERE entity_id = ANY(%s::BIGINT[])
        ON CONFLICT (entity_id, normalized_alias) DO NOTHING
        """,
        (target_id, source_ids),
    )
    cur.execute("UPDATE entity_identifiers SET entity_id = %s WHERE entity_id = ANY(%s::BIGINT[])", (target_id, source_ids))
    if not target["normalized_id"]:
        primary = next((normalize_external_id(source["normalized_id"]) for source in sources if source["normalized_id"]), None)
        if primary:
            cur.execute("UPDATE entities SET normalized_id = %s WHERE id = %s", (primary, target_id))
            target["normalized_id"] = primary
    if any(source["entity_type"] == "Drug" for source in sources):
        cur.execute("UPDATE entities SET entity_type = 'Drug' WHERE id = %s AND entity_type = 'Compound'", (target_id,))
    cur.execute("DELETE FROM entities WHERE id = ANY(%s::BIGINT[])", (source_ids,))


def _reconcile_exact_names(cur, existing, identity):
    if identity.local_label or identity.context_ambiguous or identity.document_scoped:
        return existing
    # A bare short code is insufficient proof for merging existing global nodes.
    if len(identity.normalized_alias) < 5:
        return existing
    cur.execute(
        """
        SELECT id, canonical_name, normalized_id, entity_type FROM entities entity
        WHERE entity_type = ANY(%s::TEXT[])
          AND EXISTS (SELECT 1 FROM entity_aliases alias WHERE alias.entity_id = entity.id AND alias.normalized_alias = ANY(%s::TEXT[]))
        ORDER BY id LIMIT 10
        """,
        (compatible_entity_types(identity.entity_type), [identity.normalized_alias, normalize_alias(identity.canonical_name)]),
    )
    rows = cur.fetchall()
    if len(rows) == 10:
        return existing
    candidates = [row for row in rows if normalize_entity_alias(row["canonical_name"], identity.entity_type) == identity.normalized_alias]
    if not any(row["id"] == existing["id"] for row in candidates):
        return existing
    if len(candidates) < 2:
        return existing
    namespaces = {}
    for candidate in candidates:
        for value in _candidate_identifiers(cur, candidate):
            namespaces.setdefault(identifier_namespace(value), set()).add(value.casefold())
    if any(len(values) > 1 for values in namespaces.values()):
        return existing
    target = min(candidates, key=lambda row: row["id"])
    _merge_entities(cur, target, [row for row in candidates if row["id"] != target["id"]])
    return target


def lock_graph_projection(cur, reconcile_existing=False):
    lock = "pg_advisory_xact_lock" if reconcile_existing else "pg_advisory_xact_lock_shared"
    cur.execute(f"SELECT {lock}(hashtextextended(%s, 0))", ("litkg-entity-projection-v3",))


def upsert_entity(cur, identity: EntityIdentity, *, reconcile_existing: bool = False) -> int:
    lock_graph_projection(cur, reconcile_existing)
    family = entity_family(identity.entity_type)
    for lock_key in sorted(_identity_locks(identity)):
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )

    existing = None
    if identity.normalized_id:
        cur.execute(
            """
            SELECT entity.id, entity.canonical_name, entity.normalized_id, entity.entity_type
            FROM entity_identifiers identifier
            JOIN entities entity ON entity.id = identifier.entity_id
            WHERE identifier.entity_family = %s AND identifier.identifier_key = %s
            """,
            (family, identity.normalized_id.casefold()),
        )
        existing = cur.fetchone()
    if not existing:
        cur.execute(
            "SELECT id, canonical_name, normalized_id, entity_type FROM entities WHERE canonical_key = %s",
            (identity.canonical_key,),
        )
        existing = cur.fetchone()
    if not existing and not identity.document_scoped and not identity.context_ambiguous and not identity.local_label:
        # A document-defined abbreviation must match its expansion, never a
        # globally shared short symbol such as AR with another definition.
        lookup_keys = {
            identity.normalized_alias,
            normalize_alias(identity.canonical_name),
        }
        cur.execute(
            """
            SELECT DISTINCT entity.id, entity.canonical_name, entity.normalized_id, entity.entity_type
            FROM entities entity
            JOIN entity_aliases alias ON alias.entity_id = entity.id
            WHERE entity.entity_type = ANY(%s::TEXT[])
              AND alias.normalized_alias = ANY(%s::TEXT[])
            ORDER BY entity.id
            LIMIT 3
            """,
            (compatible_entity_types(identity.entity_type), sorted(lookup_keys)),
        )
        alias_candidates = cur.fetchall()
        candidate_ids = [(candidate, _candidate_identifiers(cur, candidate)) for candidate in alias_candidates]
        if identity.normalized_id:
            exact = [candidate for candidate, values in candidate_ids if identity.normalized_id in values]
            if len(exact) == 1:
                existing = exact[0]
        if len(alias_candidates) == 1:
            candidate, values = candidate_ids[0]
            if not identity.normalized_id:
                existing = candidate
            elif not any(
                identifier_namespace(value) == identifier_namespace(identity.normalized_id)
                and value.casefold() != identity.normalized_id.casefold()
                for value in values
            ):
                # Different namespaces may identify the same exact name. Two
                # conflicting IDs from one namespace must remain separate.
                existing = candidate

    if existing:
        if reconcile_existing:
            existing = _reconcile_exact_names(cur, existing, identity)
        entity_id = existing["id"]
        cur.execute(
            """
            UPDATE entities
            SET canonical_key = CASE WHEN normalized_id IS NULL AND %s::TEXT IS NOT NULL THEN %s ELSE canonical_key END,
                normalized_id = COALESCE(normalized_id, %s),
                entity_type = CASE WHEN entity_type = 'Compound' AND %s = 'Drug' THEN 'Drug' ELSE entity_type END,
                updated_at = now()
            WHERE id = %s
            """,
            (identity.normalized_id, identity.canonical_key, identity.normalized_id, identity.entity_type, entity_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO entities (canonical_key, canonical_name, entity_type, normalized_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (canonical_key) DO UPDATE SET updated_at = now()
            RETURNING id
            """,
            (identity.canonical_key, identity.canonical_name, identity.entity_type, identity.normalized_id),
        )
        entity_id = cur.fetchone()["id"]

    if identity.normalized_id:
        cur.execute(
            """
            INSERT INTO entity_identifiers (entity_id, entity_family, normalized_id, identifier_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_family, identifier_key) DO NOTHING
            """,
            (entity_id, family, identity.normalized_id, identity.normalized_id.casefold()),
        )
    for alias in identity.aliases or (identity.canonical_name,):
        keys = {normalize_alias(alias), normalize_entity_alias(alias, identity.entity_type)}
        for key in sorted(keys):
            cur.execute(
                """
                INSERT INTO entity_aliases (entity_id, alias, normalized_alias)
                VALUES (%s, %s, %s)
                ON CONFLICT (entity_id, normalized_alias) DO NOTHING
                """,
                (entity_id, alias, key),
            )
    return entity_id


def _claim_projection(cur, claim: Claim, abbreviations=None, document_id=None, *, reconcile_existing=False) -> dict[str, Any]:
    lock_graph_projection(cur, reconcile_existing)
    subject = entity_identity(
        claim.subject.text,
        claim.subject.type,
        claim.subject.normalized_id,
        abbreviations=abbreviations,
        document_id=document_id,
    )
    object_ = entity_identity(
        claim.object.text,
        claim.object.type,
        claim.object.normalized_id,
        abbreviations=abbreviations,
        document_id=document_id,
    )
    # Acquire both endpoints' locks in a stable order, including aliases, before
    # either upsert. Opposite-direction claims cannot invert the lock order.
    extra_identities = [(p, entity_identity(p.entity.text, p.entity.type, p.entity.normalized_id, abbreviations, document_id)) for p in normalized_participants(claim)]
    keys = _identity_locks(subject) | _identity_locks(object_)
    for _, identity in extra_identities:
        keys |= _identity_locks(identity)
    for key in sorted(keys):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    subject_entity_id = upsert_entity(cur, subject, reconcile_existing=reconcile_existing)
    object_entity_id = upsert_entity(cur, object_, reconcile_existing=reconcile_existing)
    predicate = normalize_predicate(claim.predicate)
    participants = [
        {"role": "subject", "entity_id": subject_entity_id, "mention": claim.subject.mention or claim.subject.text, "entity": claim.subject},
        {"role": "object", "entity_id": object_entity_id, "mention": claim.object.mention or claim.object.text, "entity": claim.object},
    ]
    for participant, identity in extra_identities:
        participants.append({"role": participant.role, "entity_id": upsert_entity(cur, identity, reconcile_existing=reconcile_existing),
                             "mention": participant.entity.mention or participant.entity.text, "entity": participant.entity})
    qualifiers = fact_qualifiers(claim)
    if extra_identities:
        qualifiers["participants"] = sorted({(p["role"], p["entity_id"]) for p in participants if p["role"] not in {"subject", "object"}})
    evidence_text = clean_text(claim.evidence.sentence)

    return {
        "subject": subject,
        "participants": participants,
        "object": object_,
        "subject_entity_id": subject_entity_id,
        "object_entity_id": object_entity_id,
        "predicate": predicate,
        "qualifiers": qualifiers,
        "qualifiers_hash": qualifiers_fingerprint(qualifiers),
        "evidence_text": evidence_text,
        "fingerprint": digest([claim_fingerprint(
            subject=replace(subject, canonical_key=f"entity:{subject_entity_id}"),
            predicate=predicate,
            object_=replace(object_, canonical_key=f"entity:{object_entity_id}"),
            qualifiers=qualifiers,
            evidence_text=evidence_text,
            negated=claim.negated,
            speculative=claim.speculative,
        ), [e.model_dump() for e in claim.additional_evidence]]),
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
    abbreviations: dict[str, str | None] | None = None,
    original_claim_json: dict | None = None,
) -> tuple[int, int]:
    projection = _claim_projection(cur, claim, abbreviations, document_id)
    claim_json = original_claim_json if original_claim_json is not None else claim.model_dump(mode="json")

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
    cur.execute("""UPDATE claims SET catalog_version=(SELECT catalog_version FROM documents WHERE id=%s),
        processing_version=%s WHERE id=%s""", (document_id,PROCESSING_VERSION,claim_id))
    relation_id = _upsert_relation_evidence(
        cur,
        claim_id=claim_id,
        projection=projection,
        negated=claim.negated,
        speculative=claim.speculative,
    )
    store_fact(cur, claim_id, relation_id, claim, projection, document_id)
    return claim_id, relation_id


def project_existing_claim(cur, claim_id: int, claim_json: dict[str, Any], abbreviations=None, document_id=None) -> int:
    if document_id is None:
        cur.execute("SELECT document_id FROM claims WHERE id = %s", (claim_id,))
        document_id = cur.fetchone()["document_id"]
    claim = Claim.model_validate(claim_json)
    cur.execute("""SELECT d.catalog_version,d.catalog_status,ch.id,ch.text,c.catalog_version AS claim_catalog_version
        FROM claims c JOIN documents d ON d.id=c.document_id JOIN chunks ch ON ch.id=c.chunk_id
        WHERE c.id=%s""", (claim_id,))
    source = cur.fetchone()
    if source and source['claim_catalog_version'] != source['catalog_version']:
        raise ValueError('Stale extraction version; re-extraction is required')
    if source and source['catalog_status'] == 'done':
        entries = load_catalog(cur, document_id, source['catalog_version'])
        resolve_bundle(ClaimBundle(claims=[claim]), entries, source, source['catalog_version'])
    projection = _claim_projection(cur, claim, abbreviations, document_id, reconcile_existing=True)

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
    relation_id = _upsert_relation_evidence(
        cur,
        claim_id=claim_id,
        projection=projection,
        negated=claim.negated,
        speculative=claim.speculative,
    )
    store_fact(cur, claim_id, relation_id, claim, projection, document_id)
    return relation_id


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
        AND NOT EXISTS (
            SELECT 1 FROM claims c
            WHERE c.subject_entity_id = e.id OR c.object_entity_id = e.id
        )
        AND NOT EXISTS (SELECT 1 FROM fact_participants p WHERE p.entity_id = e.id)
        AND NOT EXISTS (SELECT 1 FROM document_entities d WHERE d.entity_id = e.id)
        """
    )
    deleted_entities = cur.rowcount
    return {
        "deleted_relations": deleted_relations,
        "deleted_entities": deleted_entities,
    }
