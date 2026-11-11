import asyncio
from dataclasses import dataclass
from datetime import date

import httpx

from app.core.config import Settings
from app.prompts.research_prompts import build_research_messages
from app.schemas.research import ResearchSource
from app.schemas.search import SearchResult
from app.services.citations import validate_citations
from app.services.ollama_service import OllamaService
from app.services.search_service import SearchService
from app.services.source_ranking import classify, domain_of, rank_and_dedupe
from app.services.web_extraction_service import PageDocument, fetch_document

NO_INFO_ANSWER = "No encuentro esa información en las fuentes."


@dataclass(frozen=True)
class ResearchAnswer:
    question: str
    answer: str
    sources: list[ResearchSource]


def to_sources(documents: list[PageDocument], cited: list[int]) -> list[ResearchSource]:
    return [
        ResearchSource(
            index=index,
            title=doc.title,
            url=doc.url,
            domain=doc.domain,
            category=classify(domain_of(doc.url)),
            published_date=doc.published_date,
            cited=index in cited,
        )
        for index, doc in enumerate(documents, 1)
    ]


class ResearchService:
    """Búsqueda → selección de fuentes → descarga y extracción → respuesta con citas."""

    def __init__(
        self,
        settings: Settings,
        search: SearchService,
        ollama: OllamaService,
        client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._search = search
        self._ollama = ollama
        self._client = client

    async def gather(self, question: str) -> list[PageDocument]:
        results: list[SearchResult] = await self._search.search(question, limit=10)
        ranked = rank_and_dedupe(results, max_total=self._settings.research_max_sources)
        semaphore = asyncio.Semaphore(self._settings.research_fetch_concurrency)

        async def load(result: SearchResult) -> PageDocument | None:
            async with semaphore:
                return await fetch_document(self._client, result.url, self._settings)

        documents = await asyncio.gather(*(load(r) for r in ranked))
        return [doc for doc in documents if doc is not None]

    async def answer(self, question: str) -> ResearchAnswer:
        documents = await self.gather(question)
        if not documents:
            return ResearchAnswer(question=question, answer=NO_INFO_ANSWER, sources=[])

        messages = build_research_messages(question, documents, date.today())
        raw = await self._ollama.chat(messages, temperature=0.1, max_tokens=700)
        text, cited = validate_citations(raw, len(documents))
        return ResearchAnswer(
            question=question,
            answer=text,
            sources=to_sources(documents, cited),
        )
