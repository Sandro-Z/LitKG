from collections import defaultdict
from typing import Any

from app.normalization import normalize_entity_type, normalize_predicate


def _scope_clause(
    *,
    knowledge_base_id: int | None,
    document_id: int | None,
) -> tuple[str, list[Any]]:
    if (knowledge_base_id is None) == (document_id is None):
        raise ValueError(
            "Exactly one of knowledge_base_id or document_id must be provided"
        )

    if knowledge_base_id is not None:
        return (
            """
            JOIN knowledge_base_documents kbd
              ON kbd.document_id = c.document_id
            WHERE kbd.knowledge_base_id = %s
            """,
            [knowledge_base_id],
        )
    return ("WHERE c.document_id = %s", [document_id])


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
) -> dict[str, Any]:
    scope_sql, scope_params = _scope_clause(
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
    )
    relation_filters = []
    relation_params: list[Any] = []

    if predicate:
        relation_filters.append("r.predicate = %s")
        relation_params.append(normalize_predicate(predicate))
    if entity_type:
        canonical_type = normalize_entity_type(entity_type)
        relation_filters.append(
            "(subject.entity_type = %s OR object.entity_type = %s)"
        )
        relation_params.extend([canonical_type, canonical_type])
    if not include_negated:
        relation_filters.append("r.negated = false")
    if not include_speculative:
        relation_filters.append("r.speculative = false")

    filter_sql = ""
    if relation_filters:
        filter_sql = "WHERE " + " AND ".join(relation_filters)

    cur.execute(
        f"""
        WITH raw_scoped_evidence AS (
            SELECT
                re.relation_id,
                c.id AS claim_id,
                c.document_id,
                c.evidence_text,
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
        ORDER BY
            document_count DESC,
            evidence_count DESC,
            confidence DESC NULLS LAST,
            r.id
        LIMIT %s
        """,
        (*scope_params, *relation_params, min_document_count, limit),
    )
    relation_rows = cur.fetchall()

    if not relation_rows:
        return {
            "nodes": [],
            "edges": [],
            "summary": {
                "node_count": 0,
                "relation_count": 0,
                "represented_document_count": 0,
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
                ch.section_title,
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
            section_title,
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
                "section": evidence["section_title"],
                "sentence": evidence["evidence_text"],
                "confidence": evidence["confidence"],
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
                "speculative": row["speculative"],
                "evidence_count": row["evidence_count"],
                "document_count": row["document_count"],
                "evidence": evidence[0]["sentence"] if evidence else None,
                "evidence_samples": evidence,
            }
        )

    return {
        "nodes": list(node_map.values()),
        "edges": edges,
        "summary": {
            "node_count": len(node_map),
            "relation_count": len(edges),
            "represented_document_count": represented_document_count,
        },
    }
