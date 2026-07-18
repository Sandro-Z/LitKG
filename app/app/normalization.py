import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


ENTITY_TYPES = {
    "assay": "Assay",
    "cellline": "CellLine",
    "cell_line": "CellLine",
    "compound": "Compound",
    "disease": "Disease",
    "drug": "Drug",
    "gene": "Gene",
    "mutation": "Mutation",
    "organism": "Organism",
    "other": "Other",
    "pathway": "Pathway",
    "protein": "Protein",
}

_DASHES = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


@dataclass(frozen=True)
class EntityIdentity:
    canonical_key: str
    canonical_name: str
    entity_type: str
    normalized_id: str | None
    normalized_alias: str


def clean_text(value: str | None, fallback: str = "Unknown") -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(_DASHES)
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def normalize_entity_type(value: str | None) -> str:
    raw = clean_text(value, fallback="Other")
    key = re.sub(r"[\s-]+", "_", raw).casefold()
    compact_key = key.replace("_", "")
    return ENTITY_TYPES.get(key, ENTITY_TYPES.get(compact_key, raw))


def normalize_alias(value: str | None) -> str:
    text = clean_text(value)
    text = text.strip(" \t\r\n\"'`.,;:")
    return re.sub(r"\s+", " ", text).casefold()


def normalize_external_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = clean_text(value, fallback="")
    return normalized or None


def entity_identity(
    text: str | None,
    entity_type: str | None,
    normalized_id: str | None = None,
) -> EntityIdentity:
    canonical_name = clean_text(text)
    canonical_type = normalize_entity_type(entity_type)
    external_id = normalize_external_id(normalized_id)
    normalized_name = normalize_alias(canonical_name)

    if external_id:
        identity_part = f"id:{external_id.casefold()}"
    else:
        identity_part = f"name:{normalized_name}"

    return EntityIdentity(
        canonical_key=f"{canonical_type.casefold()}|{identity_part}",
        canonical_name=canonical_name,
        entity_type=canonical_type,
        normalized_id=external_id,
        normalized_alias=normalized_name,
    )


def normalize_predicate(value: str | None) -> str:
    predicate = clean_text(value, fallback="RELATED_TO")
    predicate = re.sub(r"[^0-9A-Za-z]+", "_", predicate)
    predicate = re.sub(r"_+", "_", predicate).strip("_")
    return (predicate or "RELATED_TO").upper()


def _normalize_qualifier_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return clean_text(value, fallback="")
    if isinstance(value, dict):
        return {
            str(key): _normalize_qualifier_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_qualifier_value(item) for item in value]
    return value


def canonicalize_qualifiers(qualifiers: dict[str, Any] | None) -> dict[str, Any]:
    if not qualifiers:
        return {}
    return _normalize_qualifier_value(qualifiers)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, dict):
        return {
            key: _fingerprint_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    return value


def qualifiers_fingerprint(qualifiers: dict[str, Any] | None) -> str:
    canonical = canonicalize_qualifiers(qualifiers)
    comparable = _fingerprint_value(canonical)
    return hashlib.sha256(stable_json(comparable).encode("utf-8")).hexdigest()


def claim_fingerprint(
    *,
    subject: EntityIdentity,
    predicate: str,
    object_: EntityIdentity,
    qualifiers: dict[str, Any] | None,
    evidence_text: str,
    negated: bool,
    speculative: bool,
) -> str:
    payload = {
        "subject": subject.canonical_key,
        "predicate": normalize_predicate(predicate),
        "object": object_.canonical_key,
        "qualifiers": canonicalize_qualifiers(qualifiers),
        "evidence": normalize_alias(evidence_text),
        "negated": bool(negated),
        "speculative": bool(speculative),
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
