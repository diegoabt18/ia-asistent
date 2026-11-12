import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.prompts.research_prompts import build_research_messages
from app.schemas.research import ResearchSource
from app.schemas.search import SearchResult
from app.services.evidence import select_context
from app.services.ollama_service import ModelUsage, OllamaService
from app.services.query_generator import QueryGenerator
from app.services.search_service import SearchService
from app.services.source_ranking import classify, domain_of, rank_and_dedupe
from app.services.verification_service import (
    VerificationResult,
    VerificationService,
    verification_service_from_settings,
)
from app.services.web_extraction_service import PageDocument, fetch_document, is_html_candidate

logger = logging.getLogger(__name__)

NO_INFO_ANSWER = "No encuentro esa información en las fuentes."
RESULTS_PER_QUERY = 10
CANDIDATE_FACTOR = 2  # candidatas por plaza de fuente


@dataclass(frozen=True)
class ResearchContext:
    queries: list[str]
    documents: list[PageDocument]


@dataclass(frozen=True)
class ResearchAnswer:
    question: str
    answer: str
    queries: list[str]
    sources: list[ResearchSource]
    verification: VerificationResult
    # None cuando no hubo llamada al modelo (no se encontraron fuentes).
    usage: ModelUsage | None = None


def to_sources(documents: list[PageDocument], cited: Iterable[int]) -> list[ResearchSource]:
    cited_set = set(cited)
    return [
        ResearchSource(
            index=index,
            title=doc.title,
            url=doc.url,
            domain=doc.domain,
            category=classify(domain_of(doc.url)),
            published_date=doc.published_date,
            cited=index in cited_set,
        )
        for index, doc in enumerate(documents, 1)
    ]


class ResearchService:
    """Consultas → búsqueda → selección y descarga de fuentes → respuesta → verificación."""

    def __init__(
        self,
        settings: Settings,
        search: SearchService,
        ollama: OllamaService,
        client: httpx.AsyncClient,
        queries: QueryGenerator | None = None,
        verifier: VerificationService | None = None,
        verify_slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._search = search
        self._ollama = ollama
        self._client = client
        self._queries = queries or QueryGenerator(settings.research_max_queries)
        self._verifier = verifier or verification_service_from_settings(settings)
        # Compartido por proceso (se inyecta desde app.state). Sin él, el límite es solo de esta instancia.
        self._verify_slots = verify_slots or asyncio.Semaphore(max(1, settings.verification_max_concurrent))

    async def gather(self, question: str) -> ResearchContext:
        queries = self._queries.generate(question)
        outcomes = await asyncio.gather(
            *(self._search.search(q, limit=RESULTS_PER_QUERY) for q in queries),
            return_exceptions=True,
        )

        results: list[SearchResult] = []
        failures: list[UpstreamServiceError] = []
        for outcome in outcomes:
            if isinstance(outcome, UpstreamServiceError):
                failures.append(outcome)
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.extend(outcome)

        if failures and not results:
            raise failures[0]
        if failures:
            logger.warning("%d de %d búsquedas fallaron; se usan los resultados restantes", len(failures), len(queries))

        max_sources = self._settings.research_max_sources
        # Más candidatas que plazas: una descarga fallida no deja la respuesta sin fuentes.
        ranked = [
            r
            for r in rank_and_dedupe(results, max_total=max_sources * CANDIDATE_FACTOR)
            if is_html_candidate(r.url)
        ]
        semaphore = asyncio.Semaphore(self._settings.research_fetch_concurrency)

        async def load(result: SearchResult) -> PageDocument | None:
            async with semaphore:
                return await fetch_document(self._client, result.url, self._settings)

        # Se descarga por tandas de `max_sources` hasta reunir esa cantidad de páginas con texto.
        documents: list[PageDocument] = []
        for start in range(0, len(ranked), max_sources):
            if len(documents) >= max_sources:
                break
            batch = ranked[start : start + max_sources]
            loaded = await asyncio.gather(*(load(r) for r in batch))
            documents.extend(doc for doc in loaded if doc is not None)
        documents = documents[:max_sources]
        return ResearchContext(queries=queries, documents=documents)

    def verify(self, answer: str, documents: list[PageDocument]) -> VerificationResult:
        return self._verifier.verify(answer, documents, ignore=(NO_INFO_ANSWER,))

    async def verify_limited(self, answer: str, documents: list[PageDocument]) -> VerificationResult:
        """Verifica en un hilo (CPU) con un máximo de verificaciones simultáneas configurable."""
        async with self._verify_slots:
            return await asyncio.to_thread(self.verify, answer, documents)

    def context_for(self, question: str, documents: list[PageDocument]) -> list[PageDocument]:
        """Solo los fragmentos más relevantes para la pregunta; el modelo nunca recibe páginas completas."""
        return select_context(
            question,
            documents,
            budget_chars=self._settings.research_context_max_chars,
            size=self._settings.evidence_fragment_chars,
            overlap=self._settings.evidence_fragment_overlap_chars,
        )

    async def answer(self, question: str) -> ResearchAnswer:
        context = await self.gather(question)
        if not context.documents:
            result = await self.verify_limited(NO_INFO_ANSWER, [])
            return ResearchAnswer(
                question=question,
                answer=result.answer,
                queries=context.queries,
                sources=[],
                verification=result,
            )

        prompt_docs = self.context_for(question, context.documents)
        messages = build_research_messages(question, prompt_docs, date.today())
        completion = await self._ollama.chat(messages, temperature=0.1, max_tokens=700)
        result = await self.verify_limited(completion.text, context.documents)
        return ResearchAnswer(
            question=question,
            answer=result.answer,
            queries=context.queries,
            sources=to_sources(context.documents, result.valid_source_indices),
            verification=result,
            usage=completion.usage,
        )
