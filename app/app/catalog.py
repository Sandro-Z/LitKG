"""Document-wide, versioned entity identities. No facts are inferred here."""
import hashlib
import json
import re
import unicodedata

from psycopg.types.json import Jsonb

from app.normalization import (
    abbreviation_key, entity_family, entity_identity, extract_abbreviations,
    normalize_alias, normalize_entity_alias, normalize_external_id, identifier_namespace, stable_json,
    _DASHES,
)

PROCESSING_VERSION = "litkg-v3.3"
CATALOG_VERSION = "catalog-v3-quarantine"
# The model contract is unchanged. Revalidate existing successful segment
# caches locally rather than sending the whole literature collection again.
CATALOG_CACHE_VERSION = "catalog-v2-grounded-repair"
EXTRACTION_VERSION = "claims-v4-resumable"


def digest(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def source_contains(text, quote):
    # Whitespace/layout differences are allowed; semantic paraphrases are not.
    if not quote:
        return False
    quote = normalize_alias(quote)
    if not quote:
        return False
    pattern = (r"(?<!\w)" if quote[0].isalnum() else "") + re.escape(quote) + (r"(?!\w)" if quote[-1].isalnum() else "")
    return re.search(pattern, normalize_alias(text)) is not None


def segments(chunks, max_chars=12000):
    """Cover every character, including oversized paragraphs, without slicing off a tail."""
    if max_chars < 1:
        raise ValueError('CATALOG_SEGMENT_CHARS must be positive')
    overlap = min(200, max_chars // 10)
    for chunk in chunks:
        text = chunk["text"]
        start = 0
        while start < len(text):
            end = min(len(text), start + max_chars)
            yield {**chunk, "text": text[start:end], "start": start, "end": end}
            if end == len(text):
                break
            start = end - overlap


def split_catalog_segment(segment, min_chars=500):
    """Subdivide a capacity-limited segment without dropping any source text."""
    if min_chars < 1:
        raise ValueError('CATALOG_MIN_SEGMENT_CHARS must be positive')
    text = segment['text']
    length = len(text)
    if length <= max(1, min_chars):
        return []
    middle = length // 2
    boundaries = [m.end() for m in re.finditer(r'\s+', text)
                  if length // 3 <= m.end() <= 2 * length // 3]
    cut = min(boundaries, key=lambda position: abs(position-middle)) if boundaries else middle
    overlap = min(200, length // 10)
    left_end, right_start = min(length-1, cut+overlap), max(1, cut-overlap)
    start = segment.get('start', 0)
    return [
        {**segment, 'text':text[:left_end], 'start':start, 'end':start+left_end},
        {**segment, 'text':text[right_start:], 'start':start+right_start, 'end':start+length},
    ]


class CatalogValidationError(ValueError):
    """A source-grounding failure eligible for bounded model correction."""


def source_span(text, quote):
    """Locate a literal quote after the same layout normalization as validation.

    Keep offsets into the original text, including whitespace and Unicode
    ligatures. No fuzzy matching, word expansion, or identity-changing hyphen
    removal is performed.
    """
    if not quote:
        return None
    literal = (r'(?<!\w)' if quote[0].isalnum() else '') + re.escape(quote) + (r'(?!\w)' if quote[-1].isalnum() else '')
    exact = re.search(literal, text)
    if exact:
        return exact.span()
    needle = normalize_alias(quote)
    normalized, offsets = [], []
    index = 0
    while index < len(text):
        end = index + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        for char in unicodedata.normalize('NFKC', text[index:end]).translate(_DASHES).casefold():
            if char.isspace():
                if normalized and normalized[-1] == ' ':
                    offsets[-1] = (offsets[-1][0], end)
                    continue
                char = ' '
            normalized.append(char)
            offsets.append((index, end))
        index = end
    if not needle:
        return None
    pattern = (r'(?<!\w)' if needle[0].isalnum() else '') + re.escape(needle) + (r'(?!\w)' if needle[-1].isalnum() else '')
    match = re.search(pattern, ''.join(normalized))
    if not match:
        return None
    return offsets[match.start()][0], offsets[match.end() - 1][1]


def _catalog_sources(segment):
    return [source for source in segment.get('sources', [segment['text']]) if source]


def prepare_catalog(bundle, segment, *, quarantine=False):
    """Keep grounded entries and explicitly isolate unsupported candidates.

    Strict callers still get an exception. Pipeline callers may quarantine
    individual entries, but an entirely invalid nonempty response still fails
    and is eligible for correction. Exclusions never become entity identities.
    """
    if not quarantine:
        return _prepare_catalog(bundle, segment)
    result = bundle.model_copy(deep=True)
    result.entities = []
    repairs, rejections = list(bundle._catalog_repairs), list(bundle._catalog_rejections)
    for index, entity in enumerate(bundle.entities):
        try:
            prepared = _prepare_catalog(type(bundle)(entities=[entity]), segment)
        except CatalogValidationError as exc:
            rejections.append({'entity_index': index, 'entity': entity.model_dump(mode='json'),
                               'error': str(exc).replace('entity[0]', f'entity[{index}]')})
        else:
            result.entities.extend(prepared.entities)
            repairs.extend({**repair, 'entity_index': index} for repair in prepared._catalog_repairs)
    if rejections and not result.entities:
        raise CatalogValidationError(rejections[0]['error'] + '; no grounded catalog entries remain')
    result._catalog_repairs = repairs
    result._catalog_rejections = rejections
    return result


def _prepare_catalog(bundle, segment):
    """Repair only fields that can be anchored to actual source characters.

    Unsupported aliases are discarded with an audit record. A missing or
    paraphrased evidence sentence is replaced by a source window containing
    the verified mention. Unsupported core mentions and identifiers still fail.
    """
    result = bundle.model_copy(deep=True)
    repairs = list(bundle._catalog_repairs)
    sources = _catalog_sources(segment)

    def locate(quote):
        for source in sources:
            span = source_span(source, quote)
            if span is not None:
                return source, span
        return None

    for index, entity in enumerate(result.entities):
        mention = entity.mention or entity.text
        located = locate(mention)
        if located is None and entity.mention:
            located = locate(entity.text)
        if located is None:
            raise CatalogValidationError(f'Catalog mention is absent from its source segment (entity[{index}])')
        source, (start, end) = located
        entity.mention = source[start:end]
        if entity.mention != mention:
            repairs.append({'entity_index': index, 'field': 'mention', 'original': mention, 'replacement': entity.mention})
        grounded_evidence = locate(entity.evidence)
        if grounded_evidence is not None:
            evidence_source, (evidence_start, evidence_end) = grounded_evidence
            evidence = evidence_source[evidence_start:evidence_end]
        else:
            evidence = None
        if evidence is None or not source_contains(evidence, entity.mention):
            # Use an exact source window, never a synthesized sentence. Newlines
            # also stop a window from joining a title, heading and body.
            left = max(source.rfind(mark, 0, start) for mark in '.!?\n') + 1
            endings = [pos + 1 for mark in '.!?\n' if (pos := source.find(mark, end)) >= 0]
            right = min(endings) if endings else len(source)
            left, right = max(left, start - 500), min(right, end + 500)
            evidence = source[left:right].strip()
        if entity.evidence != evidence:
            repairs.append({'entity_index': index, 'field': 'evidence', 'original': entity.evidence, 'replacement': evidence})
            entity.evidence = evidence
        aliases = []
        for alias in entity.aliases:
            location = locate(alias)
            if location is None:
                repairs.append({'entity_index': index, 'field': 'alias', 'original': alias, 'replacement': None})
                continue
            alias_source, (alias_start, alias_end) = location
            grounded_alias = alias_source[alias_start:alias_end]
            if grounded_alias not in aliases:
                aliases.append(grounded_alias)
        entity.aliases = aliases
    validate_catalog(result, segment)
    result._catalog_repairs = repairs
    return result


def validate_catalog(bundle, segment):
    sources = _catalog_sources(segment)
    for index, entity in enumerate(bundle.entities):
        if entity.type == 'Other' and not entity.uncertainty_reason:
            entity.uncertainty_reason = 'insufficient type evidence'
        if not any(source_contains(source, entity.evidence) for source in sources):
            raise CatalogValidationError(f"Catalog evidence is absent from its source segment (entity[{index}])")
        mention = entity.mention or entity.text
        if not source_contains(entity.evidence, mention):
            raise CatalogValidationError(f"Catalog mention is absent from its evidence (entity[{index}])")
        if entity.normalized_id and not any(source_contains(source, entity.normalized_id) for source in sources):
            # The model must copy the literal identifier; normalization is local.
            raise CatalogValidationError(f"Catalog standard ID was not explicitly supplied (entity[{index}])")
        if any(not any(source_contains(source, alias) for source in sources) for alias in entity.aliases):
            raise CatalogValidationError(f"Catalog alias is absent from its source segment (entity[{index}])")


def merge_catalog(records, abbreviations, document_id):
    """Merge exact identities and unambiguous, document-defined aliases only."""
    entries = {}
    for entity, location in records:
        identity = entity_identity(entity.text, entity.type, entity.normalized_id, abbreviations, document_id)
        key = identity.canonical_key
        local_id = "E" + digest(key)[:24]
        entry = entries.setdefault(local_id, {
            "local_id": local_id, "text": identity.canonical_name, "type": identity.entity_type,
            "normalized_id": identity.normalized_id, "aliases": [], "locations": [],
            "identifiers": [identity.normalized_id] if identity.normalized_id else [],
            "ambiguity": entity.ambiguity or ("ambiguous abbreviation" if identity.context_ambiguous else None),
            "uncertainty_reason": entity.uncertainty_reason,
        })
        entry["aliases"] = sorted(set(entry["aliases"] + list(identity.aliases) + entity.aliases))
        if location not in entry["locations"]:
            entry["locations"].append(location)
    # Explicit, unambiguous aliases may join canonical names across segments.
    # An alias shared by several definitions cannot prove their equivalence.
    parents = {key:key for key in entries}
    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key
    def alias_key(entry, alias):
        return entity_family(entry['type']),normalize_entity_alias(alias,entry['type'])
    alias_owners = {}
    for local_id, entry in entries.items():
        for alias in entry["aliases"]:
            alias_owners.setdefault(alias_key(entry,alias), set()).add(local_id)
    for local_id,entry in sorted(entries.items()):
        owners = alias_owners.get(alias_key(entry,entry['text']),set())
        if len(owners) != 2 or entry.get('ambiguity') or (abbreviation_key(entry['text']) in abbreviations and abbreviations[abbreviation_key(entry['text'])] is None):
            continue
        if any(entries[owner].get('ambiguity') for owner in owners):
            continue
        a,b = sorted(root(owner) for owner in owners)
        if a == b:
            continue
        ids = [identifier for key,item in entries.items() if root(key) in {a,b} for identifier in item['identifiers']]
        namespaces = {}
        for identifier in ids:
            namespaces.setdefault(identifier_namespace(identifier),set()).add(identifier.casefold())
        if not any(len(values)>1 for values in namespaces.values()):
            parents[b] = a
    groups = {}
    for key,entry in entries.items():
        groups.setdefault(root(key),[]).append(entry)
    merged = {}
    for group in groups.values():
        representative = min(group,key=lambda e:(not bool(e['normalized_id']),-len(e['text']),e['local_id']))
        result = dict(representative)
        result['aliases'] = sorted({alias for entry in group for alias in [entry['text'],*entry['aliases']]})
        result['identifiers'] = sorted({identifier for entry in group for identifier in entry['identifiers']})
        result['ambiguity'] = '; '.join(sorted({e['ambiguity'] for e in group if e.get('ambiguity')})) or None
        result['uncertainty_reason'] = '; '.join(sorted({e['uncertainty_reason'] for e in group if e.get('uncertainty_reason')})) or ('insufficient type evidence' if result['type']=='Other' else None)
        result['locations'] = []
        for entry in group:
            for location in entry['locations']:
                if location not in result['locations']:
                    result['locations'].append(location)
        merged[result['local_id']] = result
    entries = merged
    alias_owners = {}
    for key,entry in entries.items():
        for alias in entry['aliases']:
            alias_owners.setdefault(alias_key(entry,alias),set()).add(key)
    for entry in entries.values():
        conflicts = [a for a in entry["aliases"] if len(alias_owners[alias_key(entry,a)]) > 1]
        if conflicts:
            entry["ambiguity"] = entry["ambiguity"] or "shared aliases: " + ", ".join(conflicts)
    return list(entries.values())


def relevant_entries(entries, text):
    # Identity hints only. Whole-document quotes/locations are neither necessary
    # for matching nor admissible evidence for this extraction segment.
    result = []
    for entry in entries:
        aliases = [alias for alias in [entry['text'], *entry['aliases']] if source_contains(text, alias)]
        if aliases:
            result.append({key:entry.get(key) for key in
                           ('local_id','text','type','normalized_id','ambiguity')}
                          | {'aliases':sorted(set(aliases))})
    return result


def directory_identity_matches(entity, entry, mention):
    names = [entry['text'], *entry['aliases']]
    identity = entity.text
    if entity_family(entity.type) != entity_family(entry['type']):
        return False
    if not any(normalize_entity_alias(identity, entry['type']) == normalize_entity_alias(name, entry['type']) for name in names):
        return False
    for name in names:
        if normalize_entity_alias(mention, entry['type']) == normalize_entity_alias(name, entry['type']):
            return True
        span = source_span(mention, name)
        if span is None:
            continue
        if normalize_alias(mention) == normalize_alias(name):
            return True
        # Permit grammatical/process context, while keeping species, mutations,
        # stereochemistry and other unknown modifiers part of the identity.
        context = normalize_alias(mention[:span[0]] + ' ' + mention[span[1]:])
        words = re.findall(r'\w+', context)
        neutral = {'the','a','an','of','level','levels','expression','secretion','activity',
                   'protein','gene','mrna','serum','plasma','concentration','concentrations',
                   'increased','decreased','high','low','amount','amounts','proportion','percentage'}
        if words and all(word in neutral for word in words):
            return True
    return False


def load_catalog(cur, document_id, version):
    cur.execute("SELECT entry FROM document_entities WHERE document_id = %s AND catalog_version = %s ORDER BY local_id", (document_id, version))
    return [row["entry"] for row in cur.fetchall()]


def add_review(cur, document_id, version, kind, detail):
    cur.execute("""
        INSERT INTO review_items (document_id, version, item_key, kind, detail)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (document_id, version, item_key) DO UPDATE SET detail = EXCLUDED.detail
    """, (document_id, version or "legacy", digest([kind, detail]), kind, Jsonb(detail)))


def resolve_bundle(bundle, entries, chunk, version):
    """Directory entries assist identity; each assertion remains chunk grounded."""
    by_id = {e["local_id"]: e for e in entries}
    omissions = []
    for claim in bundle.claims:
        from app.facts import normalized_participants
        claim.participants = normalized_participants(claim)
        for evidence in [claim.evidence, *claim.additional_evidence]:
            if not source_contains(chunk["text"], evidence.sentence):
                raise ValueError("Claim evidence is absent from the current chunk")
        for role, entity in [("subject", claim.subject), ("object", claim.object), *[(p.role, p.entity) for p in claim.participants]]:
            if entity.process and not entity.mention:
                raise ValueError("Split process requires the original mention")
            mention = entity.mention or entity.text
            if not source_contains(chunk["text"], mention):
                raise ValueError("Participant mention is absent from the current chunk")
            entry = by_id.get(entity.directory_id)
            if entity.directory_id and entry is None:
                raise ValueError("Unknown document directory ID")
            candidates = [e for e in entries if directory_identity_matches(entity, e, mention)]
            if not entry and len(candidates) == 1 and not candidates[0]["ambiguity"]:
                entry = candidates[0]
            if not entry and not candidates and any(
                entity_family(entity.type) == entity_family(e['type']) and any(
                    normalize_entity_alias(entity.text,e['type']) == normalize_entity_alias(a,e['type'])
                    for a in [e['text'],*e['aliases']]) for e in entries):
                raise ValueError('Directory identity would discard source mention modifiers')
            if entry:
                names = [entry["text"], *entry["aliases"]]
                if not directory_identity_matches(entity, entry, mention):
                    raise ValueError("Directory ID does not match the source mention")
                if entry["ambiguity"] and not source_contains(mention, entry["text"]):
                    # No guessed interpretation of an ambiguous acronym.
                    entity.directory_id = None
                    entity.uncertainty_reason = "ambiguous directory alias"
                    claim.review_reasons.append("ambiguous directory alias: " + mention)
                    entry = None
            if entry:
                if entity.normalized_id and normalize_external_id(entity.normalized_id) not in entry.get('identifiers',[entry['normalized_id']]):
                    raise ValueError("Claim ID conflicts with document directory")
                entity.mention = mention
                entity.text, entity.type = entry["text"], entry["type"]
                entity.normalized_id, entity.directory_id = entry["normalized_id"], entry["local_id"]
            else:
                if entity.normalized_id and not source_contains(chunk["text"], entity.normalized_id):
                    raise ValueError("Claim standard ID is absent from chunk and directory")
                omissions.append((entity, {"chunk_id": chunk["id"], "mention": mention, "role": role}))
            if entity.process and not entity.mention:
                raise ValueError("Split process requires the original mention")
            if not entity.process and re.search(r"\b(expression|secretion|activity)\b", entity.text, re.I):
                claim.review_reasons.append("unsplit process-bearing name: " + entity.text)
            if not entity.process and re.search(r"\b(expression|secretion|activity)\b", mention, re.I):
                claim.review_reasons.append("unsplit process-bearing mention: " + mention)
            if entity.type == "Other":
                entity.uncertainty_reason = entity.uncertainty_reason or "insufficient type evidence"
    return omissions


def archive_claims(cur, document_id, chunk_id=None):
    cur.execute("""
        SELECT c.*, ch.text AS source_text, ch.page_spans, ch.section_title,
               d.title, d.doi, d.filename, d.catalog_version
        FROM claims c JOIN chunks ch ON ch.id = c.chunk_id JOIN documents d ON d.id = c.document_id
        WHERE c.document_id = %s AND (%s::BIGINT IS NULL OR c.chunk_id = %s)
    """, (document_id, chunk_id, chunk_id))
    for row in cur.fetchall():
        cur.execute("""INSERT INTO claim_history (document_id, original_claim_id, snapshot_hash, snapshot)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (document_id, row["id"], digest(row), Jsonb(json.loads(stable_json(row)))))
