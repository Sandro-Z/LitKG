"""Shared helpers: QC-compatible hashing, JSON IO, policy access and type checks."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    """Exactly the QC algorithm (quality_control.models.digest)."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def save_json(path, value, indent=1):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def ontology(policy):
    onto = (policy or {}).get('ontology') or {}
    return onto.get('predicates') or {}, onto.get('node_types') or {}, onto.get('type_parents') or {}


def type_allowed(entity_type, allowed, parents):
    if not allowed or '*' in allowed:
        return True
    seen = set()
    while entity_type and entity_type not in seen:
        if entity_type in allowed:
            return True
        seen.add(entity_type)
        entity_type = parents.get(entity_type)
    return False


def map_entity_type(raw, mapping, node_types):
    """Map an external type to a policy node type; unknown -> Other (never guessed)."""
    target = (mapping.get(raw) or {}).get('type') if isinstance(mapping.get(raw), dict) else mapping.get(raw)
    if target in node_types:
        return target
    return 'Other' if 'Other' in node_types or not node_types else next(iter(node_types))


def norm_surface(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def load_mapping(path):
    """Mapping JSON: {"rules": {key: {"predicate": ..., "qualifiers": {...}, "review": ...}}}."""
    data = load_json(path)
    return data, digest(data)
