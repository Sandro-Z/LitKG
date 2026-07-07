"""Normalization helpers for biomedical literature knowledge extraction."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping

_ENTITY_TYPE_ALIASES = {
    "compound": "COMPOUND",
    "chemical": "COMPOUND",
    "molecule": "MOLECULE",
    "protein": "PROTEIN",
    "drug": "DRUG",
    "gene": "GENE",
    "disease": "DISEASE",
    "pathway": "PATHWAY",
    "cell_line": "CELL_LINE",
    "cell line": "CELL_LINE",
}


def normalize_entity_type(value: Any) -> str:
    """Return a stable upper-case entity type label."""
    if value is None:
        return "ENTITY"
    text = str(value).strip()
    if not text:
        return "ENTITY"
    return _ENTITY_TYPE_ALIASES.get(text.lower(), re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper())


def normalize_name(value: Any) -> str:
    """Normalize entity names for deterministic cross-paper matching."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_predicate(value: Any) -> str:
    """Normalize relation predicates while preserving readable semantics."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_") or "related_to"


def normalize_identifier_db(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text.upper()


def normalize_identifier_value(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    return text.strip()


def normalize_identifier_pairs(identifiers: Any) -> dict[str, list[str]]:
    """Normalize identifiers supplied as a dict, list of dicts, or list of strings."""
    result: dict[str, set[str]] = {}

    def add(db: Any, identifier: Any) -> None:
        norm_db = normalize_identifier_db(db)
        norm_value = normalize_identifier_value(identifier)
        if not norm_db or not norm_value:
            return
        result.setdefault(norm_db, set()).add(norm_value)

    if isinstance(identifiers, Mapping):
        for db, values in identifiers.items():
            if isinstance(values, (list, tuple, set)):
                for value in values:
                    add(db, value)
            else:
                add(db, values)
    elif isinstance(identifiers, (list, tuple, set)):
        for item in identifiers:
            if isinstance(item, Mapping):
                db = item.get("db") or item.get("database") or item.get("source") or item.get("namespace")
                value = item.get("id") or item.get("identifier") or item.get("value") or item.get("accession")
                add(db, value)
            elif isinstance(item, str) and ":" in item:
                db, value = item.split(":", 1)
                add(db, value)

    return {db: sorted(values) for db, values in sorted(result.items())}


def first_identifier_key(entity_type: str, identifiers: Any) -> str | None:
    pairs = normalize_identifier_pairs(identifiers)
    for db in sorted(pairs):
        for value in pairs[db]:
            return f"identifier:{entity_type}:{db}:{normalize_name(value) or value}"
    return None


def canonical_entity_name(entity: Mapping[str, Any]) -> str:
    for key in ("canonical_name", "name", "label", "text", "mention"):
        value = entity.get(key)
        if value:
            return str(value).strip()
    return ""


def merge_unique_strings(*values: Any) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            seen.setdefault(normalize_name(text), text)
    return sorted(seen.values(), key=str.casefold)
