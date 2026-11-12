from typing import Literal

from pydantic import BaseModel, Field


class ClaimOut(BaseModel):
    text: str
    status: Literal["supported", "contradicted", "unsupported", "no_citation"]
    cited_sources: list[int] = Field(default_factory=list)
    supporting_sources: list[int] = Field(default_factory=list)
    contradicting_sources: list[int] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class VerificationOut(BaseModel):
    status: Literal["VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED"]
    valid_citations: int
    total_citations: int
    unsupported_claims: list[str] = Field(default_factory=list)
    truncated: bool = False
    claims: list[ClaimOut] = Field(default_factory=list)
