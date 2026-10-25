from collections import defaultdict
from typing import Any, Literal

from app.normalization import normalize_entity_type, normalize_predicate
from app.graph_diagnostics import connectivity_metrics
from app.provenance import evidence_location
from app.claim_semantics import result_sql, result_view, display_negated
from app.predicate_registry import PREDICATES


def substring_pattern(query: str) -> str:
    """Treat user input as a literal substring, including SQL wildcard chars."""
    escaped = query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def current_claim_clause(alias='c', include_pending=False):
    reviewed = '' if include_pending else f" AND COALESCE({alias}.claim_json->>'semantic_status','unknown') <> 'pending'"
    return f"""EXISTS (SELECT 1 FROM documents source_document WHERE source_document.id={alias}.document_id
        AND source_document.catalog_status IN ('done','legacy')
        AND {alias}.catalog_version IS NOT DISTINCT FROM source_document.catalog_version)""" + reviewed


def _entity_search(alias: str) -> str:
    return f"""
        (
            {alias}.canonical_name ILIKE %s ESCAPE '!'
            OR {alias}.normalized_id ILIKE %s ESCAPE '!'
            OR EXISTS (
                SELECT 1 FROM entity_aliases alias
                WHERE alias.entity_id = {alias}.id
                  AND alias.alias ILIKE %s ESCAPE '!'
            )
            OR EXISTS (
                SELECT 1 FROM entity_identifiers identifier
                WHERE identifier.entity_id = {alias}.id
                  AND identifier.normalized_id ILIKE %s ESCAPE '!'
            )
        )
    """


def _scope_clause(
    *,
    knowledge_base_id: int | None,
    document_id: int | None,
    include_pending: bool = False,
) -> tuple[str, list[Any]]:
    if (knowledge_base_id is None) == (document_id is None):
        raise ValueError(
            "Exactly one of knowledge_base_id or document_id must be provided"
        )

    current_version = ' AND ' + current_claim_clause(include_pending=include_pending)
    if knowledge_base_id is not None:
        return (
            """
            JOIN knowledge_base_documents kbd
              ON kbd.document_id = c.document_id
            WHERE kbd.knowledge_base_id = %s
            """ + current_version,
            [knowledge_base_id],
        )
    return ("WHERE c.document_id = %s" + current_version, [document_id])


def fetch_graph(
    cur,
    *,
    knowledge_base_id: int | None = None,
    document_id: int | None = None,
    limit: int = 300,
    min_document_count: int = 1,
    evidence_limit: int = 3,
    predicate: str | None = None,
    entity_type: str | None = None,
    include_negated: bool = True,
    include_speculative: bool = True,
    query: str | None = None,
    entity_id: int | None = None,
    subject_entity_id: int | None = None,
    object_entity_id: int | None = None,
    subject: str | None = None,
    object_query: str | None = None,
    direction: Literal["both", "outgoing", "incoming"] = "both",
    offset: int = 0,
    include_total: bool = False,
    relation_layer: Literal['all', 'biological', 'method_sample'] = 'all',
    result_status: str | None = None,
    include_pending: bool = False,
    source_document_id: int | None = None,
) -> dict[str, Any]:
    scope_sql, scope_params = _scope_clause(
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        include_pending=include_pending,
    )
    relation_filters = []
    relation_params: list[Any] = []
    if source_document_id is not None:
        scope_sql += ' AND c.document_id=%s'
        scope_params.append(source_document_id)
    if result_status:
        scope_sql += f' AND ({result_sql("c")})=%s'
        scope_params.append(result_status)
    if not include_negated:
        scope_sql += f" AND (NOT c.negated OR (c.predicate IN ('AFFECTS','DIFFERS_BETWEEN') AND ({result_sql('c')}) IN ('no_effect_reported','not_significant')))"
    method_predicates = [name for name, spec in PREDICATES.items() if spec['group'] == 'measurement']
    if relation_layer not in {'all', 'biological', 'method_sample'}:
        raise ValueError('Unknown relation layer')
    if relation_layer != 'all':
        relation_filters.append('r.predicate ' + ('= ANY(%s::TEXT[])' if relation_layer == 'method_sample' else '<> ALL(%s::TEXT[])'))
        relation_params.append(method_predicates)

    if direction not in {"both", "outgoing", "incoming"}:
        raise ValueError("Unknown relation direction")
    if direction != "both" and entity_id is None:
        raise ValueError("entity_id is required when filtering by direction")
    if query and query.strip():
        relation_filters.append(
            f"({_entity_search('subject')} OR {_entity_search('object')} "
            "OR r.predicate ILIKE %s ESCAPE '!')"
        )
        relation_params.extend([substring_pattern(query)] * 9)
    for alias, value in (("subject", subject), ("object", object_query)):
        if value and value.strip():
            relation_filters.append(_entity_search(alias))
            relation_params.extend([substring_pattern(value)] * 4)
    if entity_id is not None:
        if direction == "outgoing":
            relation_filters.append("r.subject_entity_id = %s")
            relation_params.append(entity_id)
        elif direction == "incoming":
            relation_filters.append("r.object_entity_id = %s")
            relation_params.append(entity_id)
        else:
            relation_filters.append(
                "(r.subject_entity_id = %s OR r.object_entity_id = %s)"
            )
            relation_params.extend([entity_id, entity_id])
    if subject_entity_id is not None:
        relation_filters.append("r.subject_entity_id = %s")
        relation_params.append(subject_entity_id)
    if object_entity_id is not None:
        relation_filters.append("r.object_entity_id = %s")
        relation_params.append(object_entity_id)

    if predicate:
        relation_filters.append("r.predicate = %s")
        relation_params.append(normalize_predicate(predicate))
    if entity_type:
        canonical_type = normalize_entity_type(entity_type)
        relation_filters.append(
            "(subject.entity_type = %s OR object.entity_type = %s)"
        )
        relation_params.extend([canonical_type, canonical_type])
    if not include_speculative:
        relation_filters.append("r.speculative = false")

    filter_sql = ""
    if relation_filters:
        filter_sql = "WHERE " + " AND ".join(relation_filters)

    aggregate_sql = f"""
        WITH raw_scoped_evidence AS (
            SELECT
                re.relation_id,
                c.id AS claim_id,
                c.document_id,
                c.evidence_text,
                COALESCE(c.claim_json->>'semantic_status','unknown')='pending' AS semantic_pending,
                c.confidence
            FROM relation_evidence re
            JOIN claims c ON c.id = re.claim_id
            {scope_sql}
        ),
        scoped_evidence AS (
            SELECT DISTINCT ON (
                relation_id,
                document_id,
                evidence_text
            )
                relation_id,
                claim_id,
                document_id,
                evidence_text,
                semantic_pending,
                confidence
            FROM raw_scoped_evidence
            ORDER BY
                relation_id,
                document_id,
                evidence_text,
                confidence DESC NULLS LAST,
                claim_id
        )
        SELECT
            r.id,
            r.subject_entity_id,
            subject.canonical_name AS subject_name,
            subject.entity_type AS subject_type,
            subject.normalized_id AS subject_normalized_id,
            r.predicate,
            r.object_entity_id,
            object.canonical_name AS object_name,
            object.entity_type AS object_type,
            object.normalized_id AS object_normalized_id,
            r.qualifiers,
            r.negated,
            r.speculative,
            COUNT(DISTINCT (se.document_id, se.evidence_text))::INT
                AS evidence_count,
            COUNT(DISTINCT se.document_id)::INT AS document_count,
            AVG(se.confidence)::REAL AS confidence,
            MAX(se.confidence)::REAL AS max_confidence
            , BOOL_OR(se.semantic_pending) AS semantic_pending
        FROM relations r
        JOIN scoped_evidence se ON se.relation_id = r.id
        JOIN entities subject ON subject.id = r.subject_entity_id
        JOIN entities object ON object.id = r.object_entity_id
        {filter_sql}
        GROUP BY
            r.id,
            subject.id,
            object.id
        HAVING COUNT(DISTINCT se.document_id) >= %s
        """
    aggregate_params = (*scope_params, *relation_params, min_document_count)
    pagination = {}
    if include_total:
        cur.execute(
            f"SELECT COUNT(*)::INT AS total FROM ({aggregate_sql}) matched",
            aggregate_params,
        )
        pagination["total"] = cur.fetchone()["total"]
    cur.execute(
        f"""
        {aggregate_sql}
        ORDER BY
            document_count DESC,
            evidence_count DESC,
            confidence DESC NULLS LAST,
            r.id
        LIMIT %s OFFSET %s
        """,
        (*aggregate_params, limit, offset),
    )
    relation_rows = cur.fetchall()

    if not relation_rows:
        return {
            **pagination,
            "nodes": [],
            "edges": [],
            "summary": {
                **connectivity_metrics([]),
                "represented_document_count": 0,
                **({'matched_relation_count': pagination['total'], 'displayed_relation_count': 0,
                    'truncated': pagination['total'] > 0} if include_total else {}),
            },
        }

    relation_ids = [row["id"] for row in relation_rows]
    cur.execute(
        f"""
        SELECT COUNT(DISTINCT c.document_id)::INT AS count
        FROM relation_evidence re
        JOIN claims c ON c.id = re.claim_id
        {scope_sql}
          AND re.relation_id = ANY(%s::BIGINT[])
        """,
        (*scope_params, relation_ids),
    )
    represented_document_count = cur.fetchone()["count"]

    cur.execute(
        f"""
        WITH deduplicated AS (
            SELECT DISTINCT ON (
                re.relation_id,
                c.document_id,
                c.evidence_text
            )
                re.relation_id,
                c.id AS claim_id,
                c.document_id,
                d.filename,
                d.title,
                d.doi,
                ch.section_title,
                ch.page_start AS chunk_page_start,
                ch.page_end AS chunk_page_end,
                c.claim_json,
                c.evidence_text,
                c.confidence
            FROM relation_evidence re
            JOIN claims c ON c.id = re.claim_id
            JOIN documents d ON d.id = c.document_id
            LEFT JOIN chunks ch ON ch.id = c.chunk_id
            {scope_sql}
            AND re.relation_id = ANY(%s::BIGINT[])
            ORDER BY
                re.relation_id,
                c.document_id,
                c.evidence_text,
                c.confidence DESC NULLS LAST,
                c.id
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY relation_id
                    ORDER BY confidence DESC NULLS LAST, claim_id
                ) AS evidence_rank
            FROM deduplicated
        )
        SELECT
            relation_id,
            claim_id,
            document_id,
            filename,
            title,
            doi,
            section_title,
            chunk_page_start,
            chunk_page_end,
            claim_json,
            evidence_text,
            confidence
        FROM ranked
        WHERE evidence_rank <= %s
        ORDER BY relation_id, evidence_rank
        """,
        (*scope_params, relation_ids, evidence_limit),
    )
    evidence_rows = cur.fetchall()

    evidence_by_relation: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for evidence in evidence_rows:
        evidence_by_relation[evidence["relation_id"]].append(
            {
                "claim_id": evidence["claim_id"],
                "document_id": evidence["document_id"],
                "filename": evidence["filename"],
                **evidence_location(evidence),
                "sentence": evidence["evidence_text"],
                "confidence": evidence["confidence"],
                "participants": (evidence.get('claim_json') or {}).get('participants', []),
                "additional_evidence": (evidence.get('claim_json') or {}).get('additional_evidence', []),
                "source_kind": (evidence.get('claim_json') or {}).get('source_kind', 'unknown'),
                "semantic_status": (evidence.get('claim_json') or {}).get('semantic_status', 'unknown'),
                "review_reasons": (evidence.get('claim_json') or {}).get('review_reasons', []),
            }
        )

    node_map: dict[int, dict[str, Any]] = {}
    edges = []
    for row in relation_rows:
        node_map.setdefault(
            row["subject_entity_id"],
            {
                "id": row["subject_entity_id"],
                "label": row["subject_name"],
                "type": row["subject_type"],
                "normalized_id": row["subject_normalized_id"],
            },
        )
        node_map.setdefault(
            row["object_entity_id"],
            {
                "id": row["object_entity_id"],
                "label": row["object_name"],
                "type": row["object_type"],
                "normalized_id": row["object_normalized_id"],
            },
        )

        evidence = evidence_by_relation[row["id"]]
        edges.append(
            {
                "id": row["id"],
                "source": row["subject_entity_id"],
                "target": row["object_entity_id"],
                "label": row["predicate"],
                "predicate": row["predicate"],
                "qualifiers": row["qualifiers"],
                "confidence": row["confidence"],
                "max_confidence": row["max_confidence"],
                "negated": row["negated"],
                "display_negated": display_negated({'predicate':row['predicate'], 'negated':row['negated'], 'qualifiers':row['qualifiers']}),
                "speculative": row["speculative"],
                "evidence_count": row["evidence_count"],
                "document_count": row["document_count"],
                "evidence": evidence[0]["sentence"] if evidence else None,
                "evidence_samples": evidence,
                "result_status": result_view({'qualifiers': row['qualifiers'], 'negated': row['negated'], 'predicate': row['predicate']}),
                "review_status": 'pending' if row.get('semantic_pending') or any(s.get('semantic_status') == 'pending' for s in evidence) else 'unknown',
            }
        )

    return {
        **pagination,
        "nodes": list(node_map.values()),
        "edges": edges,
        "summary": {
            **connectivity_metrics(edges),
            "represented_document_count": represented_document_count,
            **({'matched_relation_count': pagination['total'], 'displayed_relation_count': len(edges),
                'truncated': offset + len(edges) < pagination['total'] or offset > 0} if include_total else {}),
        },
    }


def fetch_relations(
    cur,
    *,
    knowledge_base_id: int,
    limit: int = 100,
    offset: int = 0,
    **filters,
) -> dict[str, Any]:
    graph = fetch_graph(
        cur,
        knowledge_base_id=knowledge_base_id,
        limit=limit,
        offset=offset,
        include_total=True,
        **filters,
    )
    nodes = {node["id"]: node for node in graph["nodes"]}

    def entity(entity_id):
        node = nodes[entity_id]
        return {
            "id": node["id"],
            "text": node["label"],
            "type": node["type"],
            "normalized_id": node["normalized_id"],
        }

    relations = [
        {
            **{key: value for key, value in edge.items() if key not in {"source", "target", "label"}},
            "subject": entity(edge["source"]),
            "object": entity(edge["target"]),
        }
        for edge in graph["edges"]
    ]
    return {
        "knowledge_base_id": knowledge_base_id,
        "relations": relations,
        "total": graph["total"],
        "limit": limit,
        "offset": offset,
    }
