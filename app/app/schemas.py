from typing import Any, Optional, Literal
from pydantic import BaseModel, Field, PrivateAttr, field_validator, ValidationError
from app.normalization import normalize_entity_type, clean_text
import re
from app.entity_types import TYPE_DEFINITIONS
from app.processes import ProcessKind


class Process(BaseModel):
    kind: ProcessKind
    direction: Literal["increased", "decreased", "unchanged", "unspecified"] = "unspecified"


class Entity(BaseModel):
    text: str = Field(min_length=1)
    type: Optional[str] = Field(default=None, description=' | '.join(TYPE_DEFINITIONS))
    normalized_id: Optional[str] = None
    directory_id: Optional[str] = None
    mention: Optional[str] = None
    uncertainty_reason: Optional[str] = None
    process: Optional[Process] = None
    pending_merge: bool = False

    @field_validator("type")
    @classmethod
    def canonical_type(cls, value):
        return normalize_entity_type(value)


class Participant(BaseModel):
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    entity: Entity

    @field_validator('role', mode='before')
    @classmethod
    def canonical_role(cls, value):
        if isinstance(value, str):
            value = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', clean_text(value, ''))
            return re.sub(r'[\s-]+', '_', value.casefold())
        return value


class Evidence(BaseModel):
    section: Optional[str] = None
    sentence: str
    page: Optional[int] = Field(default=None, ge=1)
    page_end: Optional[int] = Field(default=None, ge=1)
    table_or_figure: Optional[str] = None


class Claim(BaseModel):
    subject: Entity
    predicate: str = Field(description="Controlled base predicate from /predicate-types; context and direction belong in qualifiers/process")
    object: Entity
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence
    confidence: float = Field(ge=0.0, le=1.0)
    negated: bool = False
    speculative: bool = False
    source_kind: Literal["primary", "cited", "background", "unknown"] = "unknown"
    result_status: Literal["observed_change", "no_effect_reported", "not_significant", "unknown"] = "unknown"
    semantic_status: Literal["accepted", "pending", "unknown"] = "unknown"
    participants: list[Participant] = Field(default_factory=list)
    additional_evidence: list[Evidence] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)


class ClaimBundle(BaseModel):
    claims: list[Claim]
    _raw_output: Optional[str] = PrivateAttr(default=None)
    _claim_repairs: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _claim_rejections: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _repair_ids: list[Any] = PrivateAttr(default_factory=list)
    _processing_gaps: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _resolution: Optional[dict[str, Any]] = PrivateAttr(default=None)
    _candidate_indices: list[int] = PrivateAttr(default_factory=list)


class CatalogEntity(Entity):
    aliases: list[str] = Field(default_factory=list)
    evidence: str = Field(min_length=1)
    ambiguity: Optional[str] = None


class CatalogBundle(BaseModel):
    entities: list[CatalogEntity]
    _raw_output: Optional[str] = PrivateAttr(default=None)
    _catalog_repairs: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _catalog_rejections: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _resolution: Optional[dict[str, Any]] = PrivateAttr(default=None)
    _candidate_indices: list[int] = PrivateAttr(default_factory=list)


def parse_model_bundle(data, schema):
    """Isolate malformed candidates before Pydantic's whole-list validation.

    Only redundant evidence and server-owned location fields may be discarded.
    A broken participant or condition invalidates its candidate, never silently
    reduces a multi-participant assertion to a different fact.
    """
    import copy
    field, item_schema, prefix, label = ('claims', Claim, 'claim', 'claim') if schema is ClaimBundle else ('entities', CatalogEntity, 'catalog', 'entity')
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        raise ValueError(f'Expected a {field} array')
    values, repairs, rejections, indices, repair_ids = [], [], [], [], []
    for index, original in enumerate(data[field]):
        item = copy.deepcopy(original)
        if isinstance(item, dict):
            entities = [item] if schema is CatalogBundle else [item.get('subject'), item.get('object')]
            if schema is ClaimBundle and isinstance(item.get('participants'), list):
                entities.extend(p.get('entity') for p in item['participants'] if isinstance(p, dict))
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                for name in ('normalized_id', 'directory_id', 'mention', 'uncertainty_reason'):
                    if entity.get(name) is not None and not isinstance(entity[name], str):
                        repairs.append({f'{label}_index': index, 'field': name, 'original': entity.pop(name), 'replacement': None})
            if schema is CatalogBundle:
                aliases = item.get('aliases', [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                    item['aliases'] = [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else []
                    repairs.append({'entity_index': index, 'field': 'aliases', 'original': aliases, 'replacement': item['aliases']})
                if not isinstance(item.get('evidence'), str) or not item['evidence'].strip():
                    item['evidence'] = item.get('mention') or item.get('text')
                    repairs.append({'entity_index': index, 'field': 'evidence', 'original': original.get('evidence'), 'replacement': item['evidence']})
        if schema is ClaimBundle and isinstance(item, dict):
            if not isinstance(item.get('additional_evidence', []), list):
                repairs.append({'claim_index': index, 'field': 'additional_evidence', 'original': item['additional_evidence'], 'replacement': []})
                item['additional_evidence'] = []
            for evidence in [item.get('evidence'), *item.get('additional_evidence', [])]:
                if isinstance(evidence, dict):
                    for name in ('page', 'page_end', 'section', 'table_or_figure'):
                        if evidence.get(name) is not None and (name in {'page', 'page_end'} or not isinstance(evidence[name], str)):
                            repairs.append({f'{label}_index': index, 'field': name, 'original': evidence.pop(name), 'replacement': None})
            if isinstance(item.get('additional_evidence'), list):
                valid = []
                for evidence in item['additional_evidence']:
                    try:
                        Evidence.model_validate(evidence)
                    except (ValueError, TypeError):
                        repairs.append({'claim_index': index, 'field': 'additional_evidence', 'original': evidence, 'replacement': None})
                    else:
                        valid.append(evidence)
                item['additional_evidence'] = valid
        try:
            values.append(item_schema.model_validate(item))
            indices.append(index)
            if schema is ClaimBundle:
                repair_ids.append(item.get('repair_id') if isinstance(item, dict) else None)
        except (ValueError, TypeError) as exc:
            error = '; '.join(f"{field}.{index}." + '.'.join(map(str, entry['loc'])) + ': ' + entry['type']
                for entry in exc.errors(include_input=False, include_url=False)) if isinstance(exc, ValidationError) else str(exc)
            rejections.append({f'{label}_index': index, label: original, 'error': error, 'outcome': 'unresolved',
                               'repair_id': original.get('repair_id') if isinstance(original, dict) else None})
    result = schema(**{field: values})
    result._candidate_indices = indices
    if schema is ClaimBundle:
        result._repair_ids = repair_ids
    setattr(result, f'_{prefix}_repairs', repairs)
    setattr(result, f'_{prefix}_rejections', rejections)
    resolution = data.get('resolution')
    if resolution is not None:
        if not isinstance(resolution, dict) or resolution.get('outcome') not in {'repaired', 'excluded', 'unresolved'} or not isinstance(resolution.get('reason'), str) or not resolution['reason'].strip():
            raise ValueError('Invalid candidate resolution')
        if resolution['outcome'] != 'repaired' and data[field]:
            raise ValueError('Excluded/unresolved resolution must have an empty candidate array')
        if resolution['outcome'] == 'repaired' and not data[field]:
            raise ValueError('Repaired resolution must include a candidate')
        result._resolution = resolution
    return result


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=12, ge=1, le=30)
    hops: int = Field(default=1, ge=0, le=2)
    generate: bool = True


class MaintenanceRequest(BaseModel):
    mode: Literal["reproject", "reextract", "reparse", "retry", "derived"]
    document_id: Optional[int] = Field(default=None, ge=1)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
