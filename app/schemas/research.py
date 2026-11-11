from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class ResearchSource(BaseModel):
    index: int
    title: str
    url: str
    domain: str
    category: str
    published_date: str | None = None
    cited: bool = False


class ResearchResponse(BaseModel):
    question: str
    answer: str
    sources: list[ResearchSource]
