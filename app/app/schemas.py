from typing import Any, Optional, Literal
from pydantic import BaseModel, Field, PrivateAttr, field_validator
from app.normalization import normalize_entity_type
from app.entity_types import TYPE_DEFINITIONS


class Process(BaseModel):
    kind: Literal["expression", "secretion", "activity"]
    direction: Literal["increased", "decreased", "unchanged", "unspecified"] = "unspecified"


class Entity(BaseModel):
    text: str = Field(min_length=1)
    type: Optional[str] = Field(default=None, description=' | '.join(TYPE_DEFINITIONS))
    normalized_id: Optional[str] = None
    directory_id: Optional[str] = None
    mention: Optional[str] = None
    uncertainty_reason: Optional[str] = None
    process: Optional[Process] = None

    @field_validator("type")
    @classmethod
    def canonical_type(cls, value):
        return normalize_entity_type(value)


class Participant(BaseModel):
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    entity: Entity


class Evidence(BaseModel):
    section: Optional[str] = None
    sentence: str
    page: Optional[int] = Field(default=None, ge=1)
    page_end: Optional[int] = Field(default=None, ge=1)
    table_or_figure: Optional[str] = None


class Claim(BaseModel):
    subject: Entity
    predicate: str
    object: Entity
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence
    confidence: float = Field(ge=0.0, le=1.0)
    negated: bool = False
    speculative: bool = False
    participants: list[Participant] = Field(default_factory=list)
    additional_evidence: list[Evidence] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)


class ClaimBundle(BaseModel):
    claims: list[Claim]
    _raw_output: Optional[str] = PrivateAttr(default=None)


class CatalogEntity(Entity):
    aliases: list[str] = Field(default_factory=list)
    evidence: str = Field(min_length=1)
    ambiguity: Optional[str] = None


class CatalogBundle(BaseModel):
    entities: list[CatalogEntity]
    _raw_output: Optional[str] = PrivateAttr(default=None)
    _catalog_repairs: list[dict[str, Any]] = PrivateAttr(default_factory=list)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=12, ge=1, le=30)
    hops: int = Field(default=1, ge=0, le=2)
    generate: bool = True


class MaintenanceRequest(BaseModel):
    mode: Literal["reproject", "reextract", "retry", "derived"]
    document_id: Optional[int] = Field(default=None, ge=1)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
