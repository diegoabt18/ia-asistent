from pydantic import BaseModel, Field

from app.schemas.verification import VerificationOut


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class ResearchSource(BaseModel):
    index: int
    title: str
    url: str
    domain: str
    category: str
    published_date: str | None = None
    # True solo si el modelo citó esta fuente con una referencia válida.
    cited: bool = False


class ResearchResponse(BaseModel):
    question: str
    answer: str
    queries: list[str] = Field(default_factory=list)
    verification: VerificationOut
    sources: list[ResearchSource]
