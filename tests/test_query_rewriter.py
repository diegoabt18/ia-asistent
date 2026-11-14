import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.prompts.research_prompts import (
    NO_INFO_ANSWER,
    format_recent_conversation,
    normalize_no_info,
)
from app.schemas.search import SearchResult
from app.services import research_service as rs
from app.services.query_generator import QueryGenerator
from app.services.query_rewriter import parse_queries, rewrite_queries
from app.services.research_service import ResearchService
from app.services.web_extraction_service import PageDocument


class FakeOllama:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, temperature, max_tokens, think=False):  # noqa: ANN001
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text, usage=None)


class FakeSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.queries: list[str] = []

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return list(self._results)


def test_parse_queries_removes_bullets_numbers_quotes_and_duplicates() -> None:
    text = '1. "temperatura actual Cúcuta"\n- temperatura actual Cúcuta\n• clima Cúcuta hoy\n\nx\n'
    assert parse_queries(text, max_queries=4) == ["temperatura actual Cúcuta", "clima Cúcuta hoy"]


def test_parse_queries_respects_the_limit_and_strips_labels() -> None:
    text = "Consulta 1: uno dos\nConsulta 2: tres cuatro\nquinto texto\nsexto texto"
    assert parse_queries(text, max_queries=2) == ["uno dos", "tres cuatro"]


def test_rewrite_sends_history_and_today_to_the_model() -> None:
    ollama = FakeOllama(text="presidente de Colombia 2026")
    history = [("user", "¿quién es el presidente?"), ("assistant", "No lo sé con certeza.")]
    queries = asyncio.run(
        rewrite_queries(ollama, "y en 2026?", history, 4, 1500, date(2026, 11, 14))  # type: ignore[arg-type]
    )
    assert queries == ["presidente de Colombia 2026"]
    system, user = ollama.calls[0]
    assert "2026-11-14" in system["content"]
    assert "Usuario: ¿quién es el presidente?" in user["content"]
    assert "Pregunta actual: y en 2026?" in user["content"]


def test_research_falls_back_to_templates_when_the_model_fails() -> None:
    settings = Settings(_env_file=None)
    service = ResearchService(
        settings,
        FakeSearch([]),  # type: ignore[arg-type]
        FakeOllama(error=UpstreamServiceError("Ollama", "caído")),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        queries=QueryGenerator(settings.research_max_queries),
    )
    queries = asyncio.run(service._build_queries("¿cuál es la capital de Francia?", []))
    assert queries == QueryGenerator(settings.research_max_queries).generate("¿cuál es la capital de Francia?")


def test_research_uses_model_queries_when_available() -> None:
    settings = Settings(_env_file=None)
    service = ResearchService(
        settings,
        FakeSearch([]),  # type: ignore[arg-type]
        FakeOllama(text="capital de Francia"),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        queries=QueryGenerator(settings.research_max_queries),
    )
    assert asyncio.run(service._build_queries("¿y su población?", [])) == ["capital de Francia"]


def test_snippet_is_used_as_evidence_when_the_page_cannot_be_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_fetch(client, url, settings):  # noqa: ANN001
        return None

    monkeypatch.setattr(rs, "fetch_document", failing_fetch)
    results = [
        SearchResult(
            title="Clima Cúcuta",
            url="https://clima.example.com/cucuta",
            snippet="Hoy en Cúcuta la temperatura es de 31 °C con cielo parcialmente nublado.",
        ),
    ]
    settings = Settings(_env_file=None, research_max_sources=2)
    service = ResearchService(
        settings,
        FakeSearch(results),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        queries=QueryGenerator(settings.research_max_queries),
    )
    context = asyncio.run(service.gather("temperatura Cúcuta"))
    assert len(context.documents) == 1
    assert isinstance(context.documents[0], PageDocument)
    assert "31 °C" in context.documents[0].text


def test_format_recent_conversation_keeps_latest_turns_in_order() -> None:
    history = [("user", "a" * 50), ("assistant", "b" * 50), ("user", "c" * 50)]
    text = format_recent_conversation(history, max_chars=120)
    assert "Usuario: " + "a" * 50 not in text
    assert text.index("Lumi: " + "b" * 50) < text.index("Usuario: " + "c" * 50)
    assert format_recent_conversation([], 100) == ""


def test_no_info_sentence_is_returned_alone() -> None:
    noisy = f"{NO_INFO_ANSWER}\n\n**Nota:** las fuentes hablan de otra cosa."
    assert normalize_no_info(noisy) == NO_INFO_ANSWER
    assert normalize_no_info("Hay 31 °C [1].") == "Hay 31 °C [1]."
