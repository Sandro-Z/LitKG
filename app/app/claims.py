"""Candidate-local source validation; incomplete assertions are never projected."""
from app.catalog import resolve_bundle, digest
from app.schemas import ClaimBundle
from app.schemas import Participant
from collections import Counter


def retains_required_context(original, corrected):
    roles = Counter(Participant.canonical_role(p['role']) for p in original.get('participants', [])
        if isinstance(p, dict) and isinstance(p.get('role'), str)) if isinstance(original.get('participants', []), list) else Counter()
    if roles - Counter(p.role for p in corrected.participants):
        return False

    def retains_fields(before, after):
        if isinstance(before, dict):
            return isinstance(after, dict) and all(key in after and retains_fields(value, after[key]) for key, value in before.items())
        if isinstance(before, list):
            return isinstance(after, list) and len(after) >= len(before) and all(retains_fields(a, b) for a, b in zip(before, after))
        return True
    return retains_fields(original.get('qualifiers') or {}, corrected.qualifiers)


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
            resolve_bundle(candidate, entries, chunk, version, repairs)
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
