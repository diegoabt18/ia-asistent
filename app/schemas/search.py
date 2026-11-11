from pydantic import BaseModel


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    engine: str | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
