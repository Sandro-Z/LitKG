from typing import Any, Optional
from pydantic import BaseModel, Field


class Entity(BaseModel):
    text: str
    type: Optional[str] = None
    normalized_id: Optional[str] = None


class Evidence(BaseModel):
    section: Optional[str] = None
    sentence: str
    page: Optional[int] = None
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


class ClaimBundle(BaseModel):
    claims: list[Claim] = Field(default_factory=list)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
