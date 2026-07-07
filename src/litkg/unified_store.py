"""SQLite-backed unified knowledge base for LitKG."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .normalization import (
    canonical_entity_name,
    first_identifier_key,
    merge_unique_strings,
    normalize_entity_type,
    normalize_identifier_pairs,
    normalize_name,
    normalize_predicate,
)


class UnifiedKnowledgeStore:
    """A global, provenance-preserving knowledge store for literature extractions.

    The store ingests paper-level extraction payloads and merges them into one
    database. Entity and relation IDs are global, while evidence rows preserve
    paper-level provenance.
    """

    def __init__(self, db_path: str | Path = "data/litkg.sqlite") -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create all tables and indexes if they do not already exist."""
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    paper_id TEXT PRIMARY KEY,
                    title TEXT,
                    doi TEXT,
                    source_path TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entities (
                    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_key TEXT NOT NULL UNIQUE,
                    entity_type TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    identifiers_json TEXT NOT NULL DEFAULT '{}',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    first_seen_paper_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(first_seen_paper_id) REFERENCES papers(paper_id)
                );

                CREATE TABLE IF NOT EXISTS mentions (
                    mention_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    mention_text TEXT NOT NULL,
                    section TEXT,
                    context TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(paper_id, entity_id, mention_text, section, context),
                    FOREIGN KEY(paper_id) REFERENCES papers(paper_id),
                    FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
                );

                CREATE TABLE IF NOT EXISTS relations (
                    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_entity_id INTEGER NOT NULL,
                    predicate TEXT NOT NULL,
                    object_entity_id INTEGER NOT NULL,
                    confidence REAL,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(subject_entity_id, predicate, object_entity_id),
                    FOREIGN KEY(subject_entity_id) REFERENCES entities(entity_id),
                    FOREIGN KEY(object_entity_id) REFERENCES entities(entity_id)
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    relation_id INTEGER NOT NULL,
                    paper_id TEXT NOT NULL,
                    evidence_text TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    section TEXT,
                    confidence REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(relation_id, paper_id, evidence_hash),
                    FOREIGN KEY(relation_id) REFERENCES relations(relation_id),
                    FOREIGN KEY(paper_id) REFERENCES papers(paper_id)
                );

                CREATE INDEX IF NOT EXISTS idx_entities_type_name
                    ON entities(entity_type, normalized_name);
                CREATE INDEX IF NOT EXISTS idx_mentions_paper
                    ON mentions(paper_id);
                CREATE INDEX IF NOT EXISTS idx_relations_subject
                    ON relations(subject_entity_id);
                CREATE INDEX IF NOT EXISTS idx_relations_object
                    ON relations(object_entity_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_relation
                    ON evidence(relation_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_paper
                    ON evidence(paper_id);
                """
            )

    def ingest_payload(self, payload: Mapping[str, Any], source_path: str | None = None) -> dict[str, Any]:
        """Ingest one paper-level extraction payload into the unified store."""
        self.initialize()
        now = _utc_now()
        summary = {
            "paper_id": None,
            "entities_seen": 0,
            "entities_created": 0,
            "relations_seen": 0,
            "relations_upserted": 0,
            "evidence_added": 0,
        }

        with self.connect() as conn:
            paper_id = self._ensure_paper(conn, payload, source_path=source_path, now=now)
            summary["paper_id"] = paper_id

            entity_ref_map: dict[str, int] = {}
            for raw_entity in _iter_entities(payload):
                if not isinstance(raw_entity, Mapping):
                    continue
                summary["entities_seen"] += 1
                entity_id, created = self._upsert_entity(conn, raw_entity, paper_id=paper_id, now=now)
                if created:
                    summary["entities_created"] += 1
                self._insert_mention(conn, paper_id, entity_id, raw_entity, now=now)
                for ref in _entity_refs(raw_entity):
                    entity_ref_map.setdefault(ref, entity_id)

            for raw_relation in _iter_relations(payload):
                if not isinstance(raw_relation, Mapping):
                    continue
                summary["relations_seen"] += 1
                relation = dict(raw_relation)
                subject_id = self._resolve_relation_endpoint(
                    conn,
                    relation.get("subject") or relation.get("source") or relation.get("head"),
                    relation.get("subject_type") or relation.get("source_type") or relation.get("head_type"),
                    paper_id,
                    entity_ref_map,
                    now,
                )
                object_id = self._resolve_relation_endpoint(
                    conn,
                    relation.get("object") or relation.get("target") or relation.get("tail"),
                    relation.get("object_type") or relation.get("target_type") or relation.get("tail_type"),
                    paper_id,
                    entity_ref_map,
                    now,
                )
                predicate = normalize_predicate(
                    relation.get("predicate") or relation.get("relation") or relation.get("type")
                )

                relation_id, upserted = self._upsert_relation(
                    conn,
                    subject_id=subject_id,
                    predicate=predicate,
                    object_id=object_id,
                    confidence=_as_float(relation.get("confidence")),
                    now=now,
                )
                if upserted:
                    summary["relations_upserted"] += 1

                evidence_added = self._insert_evidence(conn, relation_id, paper_id, relation, now=now)
                summary["evidence_added"] += int(evidence_added)
                self._refresh_relation_aggregate(conn, relation_id, now=now)

        return summary

    def export_graph(self, include_evidence: bool = True) -> dict[str, Any]:
        """Return the unified knowledge graph as a JSON-serializable dictionary."""
        self.initialize()
        with self.connect() as conn:
            nodes = []
            for row in conn.execute(
                """
                SELECT
                    e.*,
                    COUNT(DISTINCT m.paper_id) AS paper_count
                FROM entities e
                LEFT JOIN mentions m ON m.entity_id = e.entity_id
                GROUP BY e.entity_id
                ORDER BY e.entity_type, e.canonical_name
                """
            ):
                nodes.append(
                    {
                        "id": row["entity_key"],
                        "numeric_id": row["entity_id"],
                        "label": row["canonical_name"],
                        "type": row["entity_type"],
                        "normalized_name": row["normalized_name"],
                        "identifiers": _loads(row["identifiers_json"], {}),
                        "aliases": _loads(row["aliases_json"], []),
                        "paper_count": row["paper_count"],
                    }
                )

            edges = []
            relation_rows = conn.execute(
                """
                SELECT
                    r.*,
                    s.entity_key AS source_key,
                    s.canonical_name AS source_label,
                    o.entity_key AS target_key,
                    o.canonical_name AS target_label
                FROM relations r
                JOIN entities s ON s.entity_id = r.subject_entity_id
                JOIN entities o ON o.entity_id = r.object_entity_id
                ORDER BY r.predicate, s.canonical_name, o.canonical_name
                """
            ).fetchall()

            for row in relation_rows:
                evidence_rows = conn.execute(
                    """
                    SELECT ev.*, p.title, p.doi
                    FROM evidence ev
                    JOIN papers p ON p.paper_id = ev.paper_id
                    WHERE ev.relation_id = ?
                    ORDER BY ev.paper_id, ev.evidence_id
                    """,
                    (row["relation_id"],),
                ).fetchall()

                edge = {
                    "id": f"{row['source_key']}::{row['predicate']}::{row['target_key']}",
                    "numeric_id": row["relation_id"],
                    "source": row["source_key"],
                    "target": row["target_key"],
                    "source_label": row["source_label"],
                    "target_label": row["target_label"],
                    "predicate": row["predicate"],
                    "confidence": row["confidence"],
                    "evidence_count": row["evidence_count"],
                    "papers": sorted({ev["paper_id"] for ev in evidence_rows}),
                }
                if include_evidence:
                    edge["evidence"] = [
                        {
                            "paper_id": ev["paper_id"],
                            "title": ev["title"],
                            "doi": ev["doi"],
                            "text": ev["evidence_text"],
                            "section": ev["section"],
                            "confidence": ev["confidence"],
                            "metadata": _loads(ev["metadata_json"], {}),
                        }
                        for ev in evidence_rows
                    ]
                edges.append(edge)

            return {
                "metadata": {
                    "generated_at": _utc_now(),
                    "store": str(self.db_path),
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                },
                "nodes": nodes,
                "edges": edges,
            }

    def export_json(self, out_path: str | Path, include_evidence: bool = True) -> None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(self.export_graph(include_evidence=include_evidence), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def export_graphml(self, out_path: str | Path) -> None:
        """Export the unified graph as GraphML without requiring networkx."""
        import xml.etree.ElementTree as ET

        graph = self.export_graph(include_evidence=False)
        ns = "http://graphml.graphdrawing.org/xmlns"
        ET.register_namespace("", ns)
        root = ET.Element(f"{{{ns}}}graphml")

        for key_id, attr_name, attr_for, attr_type in (
            ("label", "label", "all", "string"),
            ("type", "type", "node", "string"),
            ("predicate", "predicate", "edge", "string"),
            ("confidence", "confidence", "edge", "double"),
            ("evidence_count", "evidence_count", "edge", "int"),
            ("papers", "papers", "edge", "string"),
        ):
            ET.SubElement(
                root,
                f"{{{ns}}}key",
                id=key_id,
                attrib={"attr.name": attr_name, "attr.for": attr_for, "attr.type": attr_type},
            )

        graph_el = ET.SubElement(root, f"{{{ns}}}graph", id="LitKG", edgedefault="directed")

        for node in graph["nodes"]:
            node_el = ET.SubElement(graph_el, f"{{{ns}}}node", id=node["id"])
            _graphml_data(ET, ns, node_el, "label", node["label"])
            _graphml_data(ET, ns, node_el, "type", node["type"])

        for edge in graph["edges"]:
            edge_el = ET.SubElement(graph_el, f"{{{ns}}}edge", id=edge["id"], source=edge["source"], target=edge["target"])
            _graphml_data(ET, ns, edge_el, "label", edge["predicate"])
            _graphml_data(ET, ns, edge_el, "predicate", edge["predicate"])
            if edge["confidence"] is not None:
                _graphml_data(ET, ns, edge_el, "confidence", edge["confidence"])
            _graphml_data(ET, ns, edge_el, "evidence_count", edge["evidence_count"])
            _graphml_data(ET, ns, edge_el, "papers", ",".join(edge["papers"]))

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)

    def stats(self) -> dict[str, int]:
        self.initialize()
        with self.connect() as conn:
            return {
                "papers": _count(conn, "papers"),
                "entities": _count(conn, "entities"),
                "mentions": _count(conn, "mentions"),
                "relations": _count(conn, "relations"),
                "evidence": _count(conn, "evidence"),
            }

    def _ensure_paper(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
        source_path: str | None,
        now: str,
    ) -> str:
        paper_id = str(
            payload.get("paper_id")
            or payload.get("pmid")
            or payload.get("doi")
            or payload.get("id")
            or _payload_hash(payload)
        ).strip()
        title = _optional_str(payload.get("title"))
        doi = _optional_str(payload.get("doi"))
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"entities", "relations", "triples", "paper_id", "pmid", "doi", "id", "title"}
        }
        conn.execute(
            """
            INSERT INTO papers(paper_id, title, doi, source_path, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
                title = COALESCE(excluded.title, papers.title),
                doi = COALESCE(excluded.doi, papers.doi),
                source_path = COALESCE(excluded.source_path, papers.source_path),
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (paper_id, title, doi, source_path, json.dumps(metadata, ensure_ascii=False), now, now),
        )
        return paper_id

    def _upsert_entity(
        self,
        conn: sqlite3.Connection,
        raw_entity: Mapping[str, Any],
        paper_id: str,
        now: str,
    ) -> tuple[int, bool]:
        entity_type = normalize_entity_type(raw_entity.get("type") or raw_entity.get("entity_type"))
        canonical_name = canonical_entity_name(raw_entity)
        if not canonical_name:
            canonical_name = "unknown"
        normalized = normalize_name(canonical_name)

        identifiers = normalize_identifier_pairs(raw_entity.get("identifiers") or raw_entity.get("ids"))
        entity_key = first_identifier_key(entity_type, identifiers) or f"name:{entity_type}:{normalized}"
        aliases = merge_unique_strings(raw_entity.get("aliases"), raw_entity.get("alias"))

        existing = conn.execute(
            "SELECT * FROM entities WHERE entity_key = ?",
            (entity_key,),
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO entities(
                    entity_key, entity_type, canonical_name, normalized_name,
                    identifiers_json, aliases_json, first_seen_paper_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_key,
                    entity_type,
                    canonical_name,
                    normalized,
                    json.dumps(identifiers, ensure_ascii=False, sort_keys=True),
                    json.dumps(aliases, ensure_ascii=False),
                    paper_id,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid), True

        merged_identifiers = _merge_identifier_json(existing["identifiers_json"], identifiers)
        merged_aliases = merge_unique_strings(_loads(existing["aliases_json"], []), aliases, canonical_name)
        preferred_name = existing["canonical_name"] or canonical_name

        conn.execute(
            """
            UPDATE entities
            SET
                canonical_name = ?,
                normalized_name = ?,
                identifiers_json = ?,
                aliases_json = ?,
                updated_at = ?
            WHERE entity_id = ?
            """,
            (
                preferred_name,
                normalize_name(preferred_name),
                json.dumps(merged_identifiers, ensure_ascii=False, sort_keys=True),
                json.dumps(merged_aliases, ensure_ascii=False),
                now,
                existing["entity_id"],
            ),
        )
        return int(existing["entity_id"]), False

    def _insert_mention(
        self,
        conn: sqlite3.Connection,
        paper_id: str,
        entity_id: int,
        raw_entity: Mapping[str, Any],
        now: str,
    ) -> None:
        mention_text = str(raw_entity.get("text") or raw_entity.get("mention") or canonical_entity_name(raw_entity)).strip()
        if not mention_text:
            return
        section = _optional_str(raw_entity.get("section"))
        context = _optional_str(raw_entity.get("context") or raw_entity.get("sentence"))
        conn.execute(
            """
            INSERT OR IGNORE INTO mentions(paper_id, entity_id, mention_text, section, context, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (paper_id, entity_id, mention_text, section, context, now),
        )

    def _resolve_relation_endpoint(
        self,
        conn: sqlite3.Connection,
        endpoint: Any,
        endpoint_type: Any,
        paper_id: str,
        entity_ref_map: Mapping[str, int],
        now: str,
    ) -> int:
        if isinstance(endpoint, Mapping):
            entity_id, _ = self._upsert_entity(conn, endpoint, paper_id=paper_id, now=now)
            self._insert_mention(conn, paper_id, entity_id, endpoint, now=now)
            return entity_id

        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            endpoint_text = "unknown"

        for ref in _ref_variants(endpoint_text):
            if ref in entity_ref_map:
                return entity_ref_map[ref]

        entity_type = normalize_entity_type(endpoint_type)
        normalized = normalize_name(endpoint_text)
        by_name = conn.execute(
            """
            SELECT entity_id FROM entities
            WHERE entity_type = ? AND normalized_name = ?
            ORDER BY entity_id
            LIMIT 1
            """,
            (entity_type, normalized),
        ).fetchone()
        if by_name:
            return int(by_name["entity_id"])

        entity = {
            "type": entity_type,
            "canonical_name": endpoint_text,
            "text": endpoint_text,
        }
        entity_id, _ = self._upsert_entity(conn, entity, paper_id=paper_id, now=now)
        self._insert_mention(conn, paper_id, entity_id, entity, now=now)
        return entity_id

    def _upsert_relation(
        self,
        conn: sqlite3.Connection,
        subject_id: int,
        predicate: str,
        object_id: int,
        confidence: float | None,
        now: str,
    ) -> tuple[int, bool]:
        existing = conn.execute(
            """
            SELECT relation_id FROM relations
            WHERE subject_entity_id = ? AND predicate = ? AND object_entity_id = ?
            """,
            (subject_id, predicate, object_id),
        ).fetchone()
        if existing:
            if confidence is not None:
                conn.execute(
                    "UPDATE relations SET updated_at = ? WHERE relation_id = ?",
                    (now, existing["relation_id"]),
                )
            return int(existing["relation_id"]), False

        cur = conn.execute(
            """
            INSERT INTO relations(
                subject_entity_id, predicate, object_entity_id,
                confidence, evidence_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (subject_id, predicate, object_id, confidence, now, now),
        )
        return int(cur.lastrowid), True

    def _insert_evidence(
        self,
        conn: sqlite3.Connection,
        relation_id: int,
        paper_id: str,
        relation: Mapping[str, Any],
        now: str,
    ) -> bool:
        evidence_text = str(
            relation.get("evidence")
            or relation.get("evidence_text")
            or relation.get("sentence")
            or relation.get("context")
            or ""
        ).strip()
        if not evidence_text:
            evidence_text = "no evidence text supplied"

        evidence_hash = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
        section = _optional_str(relation.get("section"))
        confidence = _as_float(relation.get("confidence"))
        metadata = {
            key: value
            for key, value in relation.items()
            if key
            not in {
                "subject",
                "source",
                "head",
                "subject_type",
                "source_type",
                "head_type",
                "object",
                "target",
                "tail",
                "object_type",
                "target_type",
                "tail_type",
                "predicate",
                "relation",
                "type",
                "evidence",
                "evidence_text",
                "sentence",
                "context",
                "section",
                "confidence",
            }
        }
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO evidence(
                relation_id, paper_id, evidence_text, evidence_hash,
                section, confidence, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation_id,
                paper_id,
                evidence_text,
                evidence_hash,
                section,
                confidence,
                json.dumps(metadata, ensure_ascii=False),
                now,
            ),
        )
        return cur.rowcount > 0

    def _refresh_relation_aggregate(self, conn: sqlite3.Connection, relation_id: int, now: str) -> None:
        stats = conn.execute(
            """
            SELECT COUNT(*) AS n, AVG(confidence) AS avg_confidence
            FROM evidence
            WHERE relation_id = ?
            """,
            (relation_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE relations
            SET evidence_count = ?, confidence = ?, updated_at = ?
            WHERE relation_id = ?
            """,
            (stats["n"], stats["avg_confidence"], now, relation_id),
        )


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Load paper extraction records from JSON or JSONL."""
    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if p.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    data = json.loads(text)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("articles"), list):
        return [item for item in data["articles"] if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"Unsupported JSON payload in {p}")


def iter_record_files(path: str | Path) -> Iterable[Path]:
    root = Path(path)
    for suffix in ("*.json", "*.jsonl"):
        yield from sorted(root.rglob(suffix))


def _iter_entities(payload: Mapping[str, Any]) -> Iterable[Any]:
    return payload.get("entities") or payload.get("nodes") or payload.get("concepts") or []


def _iter_relations(payload: Mapping[str, Any]) -> Iterable[Any]:
    return payload.get("relations") or payload.get("triples") or payload.get("edges") or []


def _entity_refs(entity: Mapping[str, Any]) -> Iterable[str]:
    for key in ("id", "local_id", "uid", "text", "mention", "canonical_name", "name", "label"):
        value = entity.get(key)
        if value:
            yield from _ref_variants(str(value))


def _ref_variants(value: str) -> Iterable[str]:
    stripped = value.strip()
    if not stripped:
        return
    yield stripped
    yield stripped.casefold()
    normalized = normalize_name(stripped)
    if normalized:
        yield normalized


def _payload_hash(payload: Mapping[str, Any]) -> str:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _merge_identifier_json(existing_json: str, incoming: Mapping[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = {}
    for source in (_loads(existing_json, {}), incoming):
        for db, values in source.items():
            merged.setdefault(db, set()).update(str(value) for value in values)
    return {db: sorted(values) for db, values in sorted(merged.items())}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _graphml_data(ET: Any, ns: str, parent: Any, key: str, value: Any) -> None:
    el = ET.SubElement(parent, f"{{{ns}}}data", key=key)
    el.text = "" if value is None else str(value)
