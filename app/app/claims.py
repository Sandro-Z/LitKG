"""Ground each assertion independently; rejected candidates remain auditable."""
from app.catalog import digest, resolve_bundle
from app.schemas import ClaimBundle


class ClaimValidationError(ValueError):
    pass


def prepare_claims(bundle, entries, segment, version):
    result = ClaimBundle(claims=[])
    result._raw_output = bundle._raw_output
    result._claim_rejections = list(bundle._claim_rejections)
    for index, claim in enumerate(bundle.claims):
        repair_id = bundle._repair_ids[index] if index < len(bundle._repair_ids) else None
        try:
            resolve_bundle(ClaimBundle(claims=[claim.model_copy(deep=True)]), entries, segment, version)
        except ValueError as exc:
            result._claim_rejections.append({'claim_index':index, 'repair_id':repair_id,
                                            'error':str(exc), 'original':claim.model_dump(mode='json')})
        else:
            result.claims.append(claim)
            result._repair_ids.append(repair_id)
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
    return result


def repair_candidates(rejections):
    return {'claims':[dict(item['original'], repair_id=str(index))
                      if isinstance(item.get('original'), dict) else {'repair_id':str(index)}
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
    result = ClaimBundle(claims=[*original.claims, *accepted])
    result._claim_rejections = [item for index,item in enumerate(original._claim_rejections)
                                if str(index) not in resolved]
    return result
