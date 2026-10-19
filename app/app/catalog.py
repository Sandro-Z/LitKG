"""Document-wide, versioned entity identities. No facts are inferred here."""
import hashlib
import json
import re
import unicodedata
from collections import Counter

from psycopg.types.json import Jsonb
from app.processes import PROCESS_CONTEXT_WORDS, PROCESS_PATTERN
from app.provenance import text_boundary

from app.normalization import (
    abbreviation_key, entity_family, entity_identity, extract_abbreviations,
    normalize_alias, normalize_entity_alias, normalize_external_id, identifier_namespace, stable_json,
    _DASHES,
    identity_modifiers,
    SPECIFIC_IDENTITY_TYPES,
)

PROCESSING_VERSION = "litkg-v3.4-proteomics"
CATALOG_VERSION = "catalog-v4-proteomics"
# Vocabulary and prompt versions are part of cache keys; the transport stays
# unchanged, but old extractions must not mask newly supported entity types.
CATALOG_CACHE_VERSION = "catalog-v2-grounded-repair"
EXTRACTION_VERSION = "claims-v5-controlled-proteomics"


def digest(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def source_contains(text, quote):
    # Whitespace/layout differences are allowed; semantic paraphrases are not.
    return source_span(text, quote) is not None


def segments(chunks, max_chars=12000, *, legacy=False):
    """Cover every character, including oversized paragraphs, without slicing off a tail."""
    if max_chars < 1:
        raise ValueError('CATALOG_SEGMENT_CHARS must be positive')
    overlap = min(200, max_chars // 10)
    for chunk in chunks:
        text = chunk["text"]
        start = 0
        while start < len(text):
            end = min(len(text), start + max_chars) if legacy else text_boundary(text, start + max_chars, start + max(1, max_chars // 2))
            yield {**chunk, "text": text[start:end], "start": start, "end": end}
            if end == len(text):
                break
            start = end - overlap if legacy else max(start + 1, text_boundary(text, end - overlap,
                max(start + 1, end - overlap - 200, end - max_chars // 2)))


def split_catalog_segment(segment, min_chars=500, *, legacy=False):
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
    left_end = min(length-1, cut+overlap) if legacy else text_boundary(text, min(length-1, cut+overlap), max(1, cut), sentences=False)
    right_start = max(1, cut-overlap) if legacy else text_boundary(text, max(1, cut-overlap), max(1, cut-overlap-200), sentences=False)
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
    # Unlike entity aliases, source quotations retain their punctuation.
    from app.normalization import clean_text
    needle = re.sub(r'\s*-\s*', '-', clean_text(quote, '').casefold())
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
    keep = [i for i, char in enumerate(normalized) if char != ' ' or not (
        (i and normalized[i-1] == '-') or (i+1 < len(normalized) and normalized[i+1] == '-'))]
    normalized, offsets = [normalized[i] for i in keep], [offsets[i] for i in keep]
    if not needle:
        return None
    pattern = (r'(?<!\w)' if needle[0].isalnum() else '') + re.escape(needle) + (r'(?!\w)' if needle[-1].isalnum() else '')
    match = re.search(pattern, ''.join(normalized))
    if not match:
        return None
    return offsets[match.start()][0], offsets[match.end() - 1][1]


def _catalog_sources(segment):
    return [source for source in segment.get('sources', [segment['text']]) if source]


def source_has_identifier(text, identifier):
    if source_contains(text, identifier):
        return True
    expected = normalize_external_id(identifier)
    explicit = re.findall(r'(?:UniProt(?:KB)?|PubChem(?:\s*CID)?|CHEBI|NCBI\s*Gene|GeneID|Entrez(?:\s*Gene)?|DrugBank|CID|(?:PSI[- ]?)?MOD|Unimod|(?:PSI[- ]?)?MS)\s*[:_]\s*[A-Z0-9]+(?:-\d+)?|https?://[^\s)\],;]+', text, flags=re.I)
    return bool(expected) and any((normalize_external_id(value) or '').casefold() == expected.casefold() for value in explicit)


def verified_alias(name, alias, entity_type, sources):
    if normalize_entity_alias(name, entity_type) == normalize_entity_alias(alias, entity_type):
        return True
    for source in sources:
        definitions = extract_abbreviations(source)
        for short, long in ((name, alias), (alias, name)):
            expansion = definitions.get(abbreviation_key(short))
            if expansion and normalize_entity_alias(expansion, entity_type) == normalize_entity_alias(long, entity_type):
                return True
    return any(source_contains(source, f'{left} ({right})')
        for source in sources for left, right in ((name, alias), (alias, name)))


def validate_site_identity(entity):
    if entity.type == 'ModificationSite' and re.fullmatch(
            r'[A-Za-z]{1,3}\s*-?\s*\d+(?:\s*[/,]\s*[A-Za-z]{0,3}\s*\d+)*', entity.text.strip()):
        raise ValueError('ModificationSite requires explicit parent protein identity; retain bare residue labels in qualifiers.site')


def prepare_catalog(bundle, segment, *, quarantine=False, allow_empty=False):
    """Keep grounded entries and explicitly isolate unsupported candidates.

    Strict callers still get an exception. Pipeline callers may quarantine
    individual entries, but an entirely invalid nonempty response still fails
    and is eligible for correction. Exclusions never become entity identities.
    """
    counts = Counter((e.text, e.type, e.mention or e.text) for e in bundle.entities)
    if len(bundle.entities) >= 8 and max(counts.values(), default=0) * 4 >= len(bundle.entities) * 3:
        raise CatalogValidationError('Repetitive catalog generation: the same entity/mention dominates the output; '
            'emit each mention once and cover the entire source, never return a truncated subset')
    if not quarantine:
        return _prepare_catalog(bundle, segment)
    result = bundle.model_copy(deep=True)
    result.entities = []
    result._candidate_indices = []
    repairs, rejections = list(bundle._catalog_repairs), list(bundle._catalog_rejections)
    for index, entity in enumerate(bundle.entities):
        index = bundle._candidate_indices[index] if bundle._candidate_indices else index
        try:
            prepared = _prepare_catalog(type(bundle)(entities=[entity]), segment)
        except CatalogValidationError as exc:
            rejections.append({'entity_index': index, 'entity': entity.model_dump(mode='json'),
                               'error': str(exc).replace('entity[0]', f'entity[{index}]')})
        else:
            result.entities.extend(prepared.entities)
            result._candidate_indices.append(index)
            repairs.extend({**repair, 'entity_index': index} for repair in prepared._catalog_repairs)
    if rejections and not result.entities and not allow_empty:
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
        try:
            validate_site_identity(entity)
        except ValueError as exc:
            raise CatalogValidationError(str(exc)) from exc
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
            if location is None or not verified_alias(entity.mention, alias, entity.type, sources):
                repairs.append({'entity_index': index, 'field': 'alias', 'original': alias, 'replacement': None})
                continue
            alias_source, (alias_start, alias_end) = location
            grounded_alias = alias_source[alias_start:alias_end]
            if grounded_alias not in aliases:
                aliases.append(grounded_alias)
        entity.aliases = aliases
        if locate(entity.text) is None or identity_modifiers(entity.text) != identity_modifiers(entity.mention):
            repairs.append({'entity_index': index, 'field': 'text', 'original': entity.text, 'replacement': entity.mention})
            entity.text = entity.mention
        if entity.normalized_id and not any(source_has_identifier(source, entity.normalized_id) for source in sources):
            repairs.append({'entity_index': index, 'field': 'normalized_id', 'original': entity.normalized_id, 'replacement': None})
            entity.normalized_id = None
    validate_catalog(result, segment)
    result._catalog_repairs = repairs
    return result


def validate_catalog(bundle, segment):
    sources = _catalog_sources(segment)
    for index, entity in enumerate(bundle.entities):
        try:
            validate_site_identity(entity)
        except ValueError as exc:
            raise CatalogValidationError(str(exc)) from exc
        if entity.type == 'Other' and not entity.uncertainty_reason:
            entity.uncertainty_reason = 'insufficient type evidence'
        if not any(source_contains(source, entity.evidence) for source in sources):
            raise CatalogValidationError(f"Catalog evidence is absent from its source segment (entity[{index}])")
        mention = entity.mention or entity.text
        if not source_contains(entity.evidence, mention):
            raise CatalogValidationError(f"Catalog mention is absent from its evidence (entity[{index}])")
        if entity.normalized_id and not any(source_has_identifier(source, entity.normalized_id) for source in sources):
            # Only deterministic formatting of an explicit source ID is allowed.
            raise CatalogValidationError(f"Catalog standard ID was not explicitly supplied (entity[{index}])")
        if any(not any(source_contains(source, alias) for source in sources)
               or not verified_alias(mention, alias, entity.type, sources) for alias in entity.aliases):
            raise CatalogValidationError(f"Catalog alias is absent from its source segment (entity[{index}])")


def merge_catalog(records, abbreviations, document_id):
    """Merge exact identities and unambiguous, document-defined aliases only."""
    entries = {}
    for entity, location in records:
        identity = entity_identity(entity.text, entity.type, entity.normalized_id, abbreviations, document_id, entity.pending_merge)
        key = identity.canonical_key
        local_id = "E" + digest(key)[:24]
        entry = entries.setdefault(local_id, {
            "local_id": local_id, "text": identity.canonical_name, "type": identity.entity_type,
            "normalized_id": normalize_external_id(entity.normalized_id) if entity.pending_merge else identity.normalized_id, "aliases": [], "locations": [],
            "identifiers": [identity.normalized_id] if identity.normalized_id else [],
            "ambiguity": entity.ambiguity or ("ambiguous abbreviation" if identity.context_ambiguous else None),
            "uncertainty_reason": entity.uncertainty_reason,
            "pending_merge": entity.pending_merge,
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
        if len(owners) != 2 or entry.get('ambiguity') or entry.get('pending_merge') or (abbreviation_key(entry['text']) in abbreviations and abbreviations[abbreviation_key(entry['text'])] is None):
            continue
        if any(entries[owner].get('ambiguity') or entries[owner].get('pending_merge') for owner in owners):
            continue
        if len({identity_modifiers(entries[owner]['text']) for owner in owners}) > 1:
            continue
        if entry['type'] in SPECIFIC_IDENTITY_TYPES and len({
                normalize_entity_alias(entries[owner]['text'], entry['type']) for owner in owners}) > 1:
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
        if words and all(word in PROCESS_CONTEXT_WORDS for word in words):
            return True
    return False


def load_catalog(cur, document_id, version):
    cur.execute("SELECT entry FROM document_entities WHERE document_id = %s AND catalog_version = %s ORDER BY local_id", (document_id, version))
    return [row["entry"] for row in cur.fetchall()]


def add_review(cur, document_id, version, kind, detail, status='pending'):
    key = [kind, detail]
    if kind in {'catalog_rejection', 'claim_rejection'}:
        label = 'entity' if kind == 'catalog_rejection' else 'claim'
        key = [kind, detail.get('chunk_id'), detail.get('start'), detail.get(label + '_index'), detail.get(label)]
    cur.execute("""
        INSERT INTO review_items (document_id, version, item_key, kind, detail, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (document_id, version, item_key) DO UPDATE
            SET detail=EXCLUDED.detail || CASE WHEN review_items.detail ? 'review_history'
                THEN jsonb_build_object('review_history',review_items.detail->'review_history') ELSE '{}'::jsonb END,
                status=CASE WHEN review_items.status='pending' THEN EXCLUDED.status ELSE review_items.status END
    """, (document_id, version or "legacy", digest(key), kind, Jsonb(detail), status))


def resolve_bundle(bundle, entries, chunk, version, repairs=None):
    """Resolve identities using complete grounded names, never a supplied ID alone."""
    from app.facts import normalized_participants
    from app.normalization import entity_family
    repairs = repairs if repairs is not None else []
    omissions = []
    for claim in bundle.claims:
        claim.participants = normalized_participants(claim)
        span = source_span(chunk['text'], claim.evidence.sentence)
        if span is None:
            raise ValueError('Claim main evidence is absent from the current chunk')
        claim.evidence.sentence = chunk['text'][span[0]:span[1]]
        additional = []
        for evidence in claim.additional_evidence:
            span = source_span(chunk['text'], evidence.sentence)
            if span is None:
                repairs.append({'field': 'additional_evidence', 'original': evidence.model_dump(), 'replacement': None})
            else:
                evidence.sentence = chunk['text'][span[0]:span[1]]
                if evidence.sentence not in [claim.evidence.sentence, *[e.sentence for e in additional]]:
                    additional.append(evidence)
        claim.additional_evidence = additional
        for evidence in [claim.evidence, *additional]:
            if evidence.table_or_figure and not source_contains(chunk['text'], evidence.table_or_figure):
                repairs.append({'field': 'table_or_figure', 'original': evidence.table_or_figure, 'replacement': None})
                evidence.table_or_figure = None
        evidence_texts = [claim.evidence.sentence, *[e.sentence for e in additional]]
        for role, entity in [('subject', claim.subject), ('object', claim.object), *[(p.role, p.entity) for p in claim.participants]]:
            validate_site_identity(entity)
            if entity.process and not entity.mention:
                raise ValueError('Split process requires the original mention')
            mention = entity.mention or entity.text
            span = source_span(chunk['text'], mention)
            if span is None:
                # A canonical directory name may be absent locally while its
                # verified abbreviation is quoted in the current evidence.
                aliases = []
                for entry in entries:
                    names = [entry['text'], *entry['aliases']]
                    if entry.get('ambiguity') or not any(normalize_entity_alias(entity.text, entry['type']) == normalize_entity_alias(name, entry['type']) for name in names):
                        continue
                    for alias in names:
                        replacement = alias
                        if entity.process:
                            name_span = source_span(mention, entity.text)
                            if name_span is None:
                                continue
                            replacement = mention[:name_span[0]] + alias + mention[name_span[1]:]
                        located = source_span(chunk['text'], replacement)
                        if located and any(source_contains(text, replacement) for text in evidence_texts):
                            aliases.append((entry['local_id'], replacement, located))
                if len({entry_id for entry_id, _, _ in aliases}) == 1:
                    _, replacement, span = max(aliases, key=lambda match: len(match[1]))
                    repairs.append({'field': 'mention', 'role': role, 'original': mention, 'replacement': replacement, 'source_span': list(span)})
                    mention = replacement
            if span is None or not any(source_contains(text, mention) for text in evidence_texts):
                raise ValueError(f'Participant {role} mention {mention!r} is absent from current chunk evidence')
            mention = chunk['text'][span[0]:span[1]]
            entity.mention = mention
            identity_mention = mention
            if entity.process:
                # Strip only the declared process, retaining species/mutations/
                # modifications in the original complete entity name.
                name_span = source_span(mention, entity.text)
                if name_span and directory_identity_matches(entity, {'text':entity.text, 'type':entity.type, 'aliases':[]}, mention):
                    identity_mention = mention[name_span[0]:name_span[1]]
                else:
                    kind = re.escape(entity.process.kind)
                    identity_mention = re.sub(r'\b(?:relative|absolute)\s+(?='+kind+r'\b)', '', mention, flags=re.I)
                    identity_mention = re.sub(r'\b(?:increased|decreased|unchanged|of|'+kind+r')\b', ' ', identity_mention, flags=re.I)
                    identity_mention = re.sub(r'\s+', ' ', identity_mention).strip()
                if not identity_mention:
                    raise ValueError('Split process has no grounded entity name')
            expanded = entity_identity(identity_mention, entity.type,
                abbreviations=chunk.get('abbreviation_map', {})).canonical_name
            candidates = [e for e in entries if not e.get('pending_merge')
                if (entity.type in {None, 'Other'} or entity_family(e['type']) == entity_family(entity.type))
                and identity_modifiers(e['text']) == identity_modifiers(expanded)
                and (any(normalize_entity_alias(identity_mention, e['type']) == normalize_entity_alias(a, e['type'])
                         for a in [e['text'], *e['aliases']]) or directory_identity_matches(entity, e, mention))]
            entry = candidates[0] if len(candidates) == 1 else None
            if entry and entry.get('ambiguity') and normalize_entity_alias(identity_mention, entry['type']) != normalize_entity_alias(entry['text'], entry['type']):
                entry = None
            if entry and entity.normalized_id and source_has_identifier(chunk['text'], entity.normalized_id):
                identifiers = [normalize_external_id(value) for value in entry.get('identifiers', [entry.get('normalized_id')]) if value]
                if normalize_external_id(entity.normalized_id) not in identifiers:
                    entry = None
            old_id = entity.directory_id
            if entry:
                if old_id != entry['local_id']:
                    repairs.append({'field': 'directory_id', 'role': role, 'original': old_id, 'replacement': entry['local_id'], 'mention': mention})
                if entity.normalized_id and normalize_external_id(entity.normalized_id) not in entry.get('identifiers', [entry['normalized_id']]):
                    repairs.append({'field': 'normalized_id', 'role': role, 'original': entity.normalized_id, 'replacement': entry['normalized_id']})
                entity.text, entity.type = entry['text'], entry['type']
                entity.normalized_id, entity.directory_id = entry['normalized_id'], entry['local_id']
                entity.pending_merge = entry.get('pending_merge', False)
            else:
                # Directory omissions and ambiguous aliases are valid source
                # entities. Keep their full names as separate pending nodes.
                entity.directory_id = None
                if old_id:
                    repairs.append({'field': 'directory_id', 'role': role, 'original': old_id, 'replacement': None,
                                    'mention': mention, 'source_span': list(span),
                                    'candidate_ids': [e['local_id'] for e in candidates]})
                entity.text = identity_mention
                if entity.normalized_id and not source_has_identifier(chunk['text'], entity.normalized_id):
                    repairs.append({'field': 'normalized_id', 'role': role, 'original': entity.normalized_id, 'replacement': None})
                    entity.normalized_id = None
                entity.pending_merge = True
                entity.uncertainty_reason = entity.uncertainty_reason or ('ambiguous directory alias' if candidates else 'pending catalog merge')
                reason = entity.uncertainty_reason + ': ' + mention
                if reason not in claim.review_reasons:
                    claim.review_reasons.append(reason)
                omissions.append((entity, {'chunk_id': chunk['id'], 'mention': mention, 'role': role}))
            validate_site_identity(entity)
            aspect_entity = entity.type not in {'BiologicalProcess', 'ProteinModification', 'Assay', 'Pathway'}
            if aspect_entity and not entity.process and PROCESS_PATTERN.search(entity.text):
                reason = 'unsplit process-bearing name: ' + entity.text
                if reason not in claim.review_reasons:
                    claim.review_reasons.append(reason)
            if aspect_entity and not entity.process and PROCESS_PATTERN.search(mention):
                reason = 'unsplit process-bearing mention: ' + mention
                if reason not in claim.review_reasons:
                    claim.review_reasons.append(reason)
            if entity.type == 'Other':
                entity.uncertainty_reason = entity.uncertainty_reason or 'insufficient type evidence'
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
