import pytest

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.schemas.search import SearchResult
from app.services import research_service
from app.services.research_service import NO_INFO_ANSWER, ResearchService
from app.services.web_extraction_service import PageDocument

QUESTION = "¿quién es el presidente de Colombia?"
OFFICIAL_URL = "https://presidencia.gov.co/inicio"
OFFICIAL_TEXT = (
    "El presidente de Colombia asumió el cargo el 7 de agosto de 2022 según el registro oficial."
)


class FakeSearch:
    """Sustituto de SearchService: responde por consulta y registra las llamadas."""

    def __init__(self, responses: dict[str, list[SearchResult] | Exception], default=None):
        self._responses = responses
        self._default = default or []
        self.calls: list[str] = []

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        value = self._responses.get(query, self._default)
        if isinstance(value, Exception):
            raise value
        return value


class FakeOllama:
    def __init__(self, reply: str | Exception = ""):
        self._reply = reply
        self.calls = 0

    async def chat(self, messages, temperature, max_tokens) -> str:
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _result(url: str) -> SearchResult:
    return SearchResult(title="Título", url=url, snippet="")


def _page(url: str, text: str) -> PageDocument:
    return PageDocument(
        url=url, domain=url.split("/")[2], title="Título", published_date=None, text=text
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def pages(monkeypatch) -> dict[str, PageDocument]:
    """Sustituye la descarga de páginas por un diccionario: no hay red en las pruebas."""
    store: dict[str, PageDocument] = {}

    async def fake_fetch(client, url, settings):
        return store.get(url)

    monkeypatch.setattr(research_service, "fetch_document", fake_fetch)
    return store


def _service(settings, search, ollama) -> ResearchService:
    return ResearchService(settings, search, ollama, client=None)


@pytest.mark.asyncio
async def test_verified_answer_with_valid_citation(settings, pages) -> None:
    pages[OFFICIAL_URL] = _page(OFFICIAL_URL, OFFICIAL_TEXT)
    search = FakeSearch({}, default=[_result(OFFICIAL_URL)])
    ollama = FakeOllama("El presidente asumió el cargo el 7 de agosto de 2022 [1].")

    result = await _service(settings, search, ollama).answer(QUESTION)

    assert result.verification.status == "VERIFIED"
    assert result.queries[0] == QUESTION
    assert len(result.queries) > 1
    assert result.sources[0].cited is True
    assert result.sources[0].category == "oficial"


@pytest.mark.asyncio
async def test_each_query_is_searched(settings, pages) -> None:
    search = FakeSearch({}, default=[])
    await _service(settings, search, FakeOllama()).answer(QUESTION)
    assert search.calls[0] == QUESTION
    assert len(search.calls) == len(set(search.calls))


@pytest.mark.asyncio
async def test_no_results_returns_unverified_without_calling_model(settings, pages) -> None:
    ollama = FakeOllama("no debería usarse")
    result = await _service(settings, FakeSearch({}, default=[]), ollama).answer(QUESTION)

    assert result.answer == NO_INFO_ANSWER
    assert result.verification.status == "UNVERIFIED"
    assert result.sources == []
    assert ollama.calls == 0


@pytest.mark.asyncio
async def test_all_searches_failing_raises_upstream_error(settings, pages) -> None:
    error = UpstreamServiceError("SearXNG", "caído")
    service = _service(settings, FakeSearch({}, default=error), FakeOllama())
    with pytest.raises(UpstreamServiceError):
        await service.answer(QUESTION)


@pytest.mark.asyncio
async def test_partial_search_failure_uses_remaining_results(settings, pages) -> None:
    pages[OFFICIAL_URL] = _page(OFFICIAL_URL, OFFICIAL_TEXT)
    search = FakeSearch({QUESTION: UpstreamServiceError("SearXNG", "timeout")}, default=[_result(OFFICIAL_URL)])
    ollama = FakeOllama("El presidente asumió el cargo el 7 de agosto de 2022 [1].")

    result = await _service(settings, search, ollama).answer(QUESTION)

    assert result.verification.status == "VERIFIED"


@pytest.mark.asyncio
async def test_ollama_error_propagates(settings, pages) -> None:
    pages[OFFICIAL_URL] = _page(OFFICIAL_URL, OFFICIAL_TEXT)
    search = FakeSearch({}, default=[_result(OFFICIAL_URL)])
    service = _service(settings, search, FakeOllama(UpstreamServiceError("Ollama", "sin respuesta")))
    with pytest.raises(UpstreamServiceError):
        await service.answer(QUESTION)


@pytest.mark.asyncio
async def test_invented_url_from_model_is_not_accepted(settings, pages) -> None:
    pages[OFFICIAL_URL] = _page(OFFICIAL_URL, OFFICIAL_TEXT)
    search = FakeSearch({}, default=[_result(OFFICIAL_URL)])
    ollama = FakeOllama("Según https://falsa.example.org/nota el presidente es otro.")

    result = await _service(settings, search, ollama).answer(QUESTION)

    assert "falsa.example.org" not in result.answer
    assert result.verification.valid_citations == 0
    assert result.verification.status == "UNVERIFIED"
