import asyncio

import pytest

from app.core.config import Settings
from app.schemas.search import SearchResult
from app.services import research_service as rs
from app.services.research_service import ResearchService
from app.services.web_extraction_service import PageDocument


class FakeSearch:
    def __init__(self, urls: list[str]) -> None:
        self._results = [SearchResult(title="t", url=u, snippet="") for u in urls]

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return list(self._results)


class FakeQueries:
    def generate(self, question: str) -> list[str]:
        return [question]


def _doc(url: str) -> PageDocument:
    return PageDocument(url=url, domain=url.split("/")[2], title="t", published_date=None, text="x" * 300)


def _service(urls: list[str], max_sources: int) -> ResearchService:
    settings = Settings(_env_file=None, research_max_sources=max_sources)
    return ResearchService(settings, FakeSearch(urls), None, None, queries=FakeQueries())  # type: ignore[arg-type]


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Registra qué URLs se descargan. Una URL que contiene 'fail' devuelve None (descarga fallida)."""
    calls: list[str] = []

    async def fake_fetch(client, url, settings):  # noqa: ANN001 - firma de fetch_document
        calls.append(url)
        return None if "fail" in url else _doc(url)

    monkeypatch.setattr(rs, "fetch_document", fake_fetch)
    return calls


def test_pdf_candidates_are_never_downloaded(fetched: list[str]) -> None:
    urls = [
        "https://a.gov.co/inicio",
        "https://b.edu.co/normas/ensayo.pdf",
        "https://c.org/pagina",
        "https://d.com/otra",
    ]
    context = asyncio.run(_service(urls, max_sources=2).gather("pregunta"))
    assert all(not u.endswith(".pdf") for u in fetched)
    assert len(context.documents) == 2


def test_failed_download_is_replaced_by_another_candidate(fetched: list[str]) -> None:
    urls = [
        "https://a.gov.co/fail",
        "https://b.edu.co/ok",
        "https://c.org/ok",
        "https://d.com/ok",
    ]
    context = asyncio.run(_service(urls, max_sources=2).gather("pregunta"))
    assert len(context.documents) == 2
    assert all("ok" in doc.url for doc in context.documents)


def test_stops_downloading_once_enough_sources_are_loaded(fetched: list[str]) -> None:
    urls = [
        "https://a.gov.co/ok",
        "https://b.edu.co/ok",
        "https://c.org/ok",
        "https://d.com/ok",
    ]
    context = asyncio.run(_service(urls, max_sources=2).gather("pregunta"))
    assert len(context.documents) == 2
    assert len(fetched) == 2
