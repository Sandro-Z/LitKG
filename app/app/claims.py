"""Candidate-local source validation; incomplete assertions are never projected."""
from app.catalog import resolve_bundle, digest
from app.schemas import ClaimBundle
from app.schemas import Participant, Entity
from app.predicate_registry import normalize_registered_claim
from app.normalization import clean_text, normalize_predicate


def normalize_comparison_groups(claim, repairs):
    if normalize_predicate(claim.predicate) != 'DIFFERS_BETWEEN':
        return
    for role in ('comparison_group', 'reference_group'):
        value = claim.qualifiers.get(role)
        if not isinstance(value, dict):
            continue
        # Compatibility with the logged entity-shaped values: preserve their
        # actual entity, normalize only the representation, never invent a group.
        if set(value) - Entity.model_fields.keys():
            raise ValueError(f'DIFFERS_BETWEEN {role} contains unsupported context fields; do not discard them')
        entity = Entity.model_validate(value)
        if entity.type != 'StudyGroup':
            raise ValueError(f'DIFFERS_BETWEEN {role} must describe a StudyGroup')
        label = entity.mention or entity.text
        participants = [p for p in claim.participants if p.role == role]
        if not participants:
            claim.participants.append(Participant(role=role, entity=entity))
        claim.qualifiers[role] = label
        repairs.append(dict(field=f'qualifiers.{role}', original=value, replacement=label))


def retains_required_context(original, corrected):
    original_participants = original.get('participants', [])
    if not isinstance(original_participants, list):
        return False

    def names(entity):
        return {clean_text(value, '').casefold() for value in (entity.get('text'), entity.get('mention'))
                if isinstance(value, str) and value.strip()}

    def same_entity(before, after):
        if not isinstance(before, dict) or not names(before) & names(after):
            return False
        if 'StudyGroup' in (before.get('type'), after.get('type')) and before.get('mention') and after.get('mention'):
            if clean_text(before['mention'], '').casefold() != clean_text(after['mention'], '').casefold():
                return False
        process = before.get('process')
        if process:
            actual = after.get('process') or {}
            if not isinstance(process, dict) or process.get('kind') != actual.get('kind'):
                return False
            if process.get('direction', 'unspecified') != 'unspecified' and process['direction'] != actual.get('direction'):
                return False
        return True

    after = corrected.model_dump(mode='json')
    # A grounded model correction may repair the one structural inversion
    # identified by endpoint types. All participants, qualifiers and polarity
    # still pass the checks below; unrelated endpoint substitutions remain invalid.
    reverse_target = (
        normalize_predicate(original.get('predicate', '')) == 'TARGETS' and corrected.predicate == 'TARGETS'
        and (original.get('subject') or {}).get('type') in {'Protein','Gene'}
        and (original.get('object') or {}).get('type') in {'Drug','Intervention'}
        and corrected.subject.type in {'Drug','Intervention'} and corrected.object.type in {'Protein','Gene'}
        and corrected.semantic_status == 'accepted'
    )
    for role in ('subject', 'object'):
        before_entity = original.get({'subject':'object','object':'subject'}[role] if reverse_target else role)
        if not same_entity(before_entity, after[role]):
            return False
    from app.predicate_registry import PREDICATE_ALIASES, DIRECTION_ALIASES, _key
    def predicate(value):
        key = _key(value) if isinstance(value, str) else ''
        return 'AFFECTS' if key in DIRECTION_ALIASES else PREDICATE_ALIASES.get(key, key)
    if predicate(original.get('predicate')) != predicate(corrected.predicate):
        return False
    key = _key(original['predicate'])
    if key in DIRECTION_ALIASES:
        direction = corrected.object.process.direction if corrected.object.process else corrected.qualifiers.get('object_direction')
        if direction not in (None, 'unspecified', DIRECTION_ALIASES[key]):
            return False
    if any(original.get(k, False) != after[k] for k in ('negated', 'speculative')):
        return False
    available = list(after['participants'])
    for participant in original_participants:
        if not isinstance(participant, dict):
            return False
        matches = [p for p in available if p['role'] == Participant.canonical_role(participant.get('role'))
                   and same_entity(participant.get('entity'), p['entity'])]
        if not matches:
            return False
        available.remove(matches[0])

    def retains_fields(before, after):
        if isinstance(before, dict):
            return isinstance(after, dict) and all(key in after and retains_fields(value, after[key]) for key, value in before.items())
        if isinstance(before, list):
            return isinstance(after, list) and len(after) >= len(before) and all(retains_fields(a, b) for a, b in zip(before, after))
        return clean_text(before, '') == clean_text(after, '') if isinstance(before, str) and isinstance(after, str) else before == after
    qualifiers = dict(original.get('qualifiers') or {})
    for role in ('comparison_group', 'reference_group'):
        if isinstance(qualifiers.get(role), dict):
            value = qualifiers[role]
            qualifiers[role] = value.get('mention') or value.get('text')
    return retains_fields(qualifiers, corrected.qualifiers)


class ClaimValidationError(ValueError):
    def __init__(self, message, bundle=None):
        super().__init__(message)
        self.bundle = bundle


def prepare_claims(bundle, entries, chunk, version):
    result = bundle.model_copy(deep=True)
    result.claims = []
    result._candidate_indices = []
    result._repair_ids = []
    for position, claim in enumerate(bundle.claims):
        index = bundle._candidate_indices[position] if bundle._candidate_indices else position
        repair_id = bundle._repair_ids[position] if position < len(bundle._repair_ids) else None
        candidate = ClaimBundle(claims=[claim.model_copy(deep=True)])
        repairs = []
        try:
            normalize_comparison_groups(candidate.claims[0], repairs)
            resolve_bundle(candidate, entries, chunk, version, repairs)
            normalize_registered_claim(candidate.claims[0], repairs)
            from app.claim_semantics import normalize_result, check_semantics
            normalize_result(candidate.claims[0], repairs)
            check_semantics(candidate.claims[0], repairs)
        except ValueError as exc:
            result._claim_rejections.append({'claim_index': index, 'claim': claim.model_dump(mode='json'),
                                             'error': str(exc), 'outcome': 'unresolved', 'repair_id': repair_id})
        else:
            result.claims.extend(candidate.claims)
            result._candidate_indices.append(index)
            result._repair_ids.append(repair_id)
            result._claim_repairs.extend({'claim_index': index, **repair} for repair in repairs)
    return result


def combine_claims(bundles):
    result = ClaimBundle(claims=[])
    seen = set()
    for bundle in bundles:
        for claim in bundle.claims:
            key = digest(claim.model_dump(mode='json'))
            if key not in seen:
                seen.add(key)
                result.claims.append(claim)
        result._claim_rejections.extend(bundle._claim_rejections)
        result._claim_repairs.extend(bundle._claim_repairs)
        result._processing_gaps.extend(bundle._processing_gaps)
    return result


def repair_candidates(rejections):
    return {'claims':[dict(item.get('claim', item.get('original')), repair_id=str(index))
                      if isinstance(item.get('claim', item.get('original')), dict) else {'repair_id':str(index)}
                      for index,item in enumerate(rejections)]}


def merge_correction(original, correction):
    """Missing, duplicate or unlabelled corrections never erase a rejection."""
    counts = {}
    for repair_id in correction._repair_ids:
        if isinstance(repair_id,str):
            counts[repair_id] = counts.get(repair_id,0) + 1
    valid = {str(i) for i in range(len(original._claim_rejections))}
    resolved, accepted = set(), []
    for claim, repair_id in zip(correction.claims, correction._repair_ids):
        if isinstance(repair_id,str) and repair_id in valid and counts[repair_id] == 1:
            resolved.add(repair_id)
            accepted.append(claim)
    result = original.model_copy(deep=True)
    result.claims.extend(accepted)
    result._claim_rejections = [item for index,item in enumerate(original._claim_rejections)
                                if str(index) not in resolved]
    return result
