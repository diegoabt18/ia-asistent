import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import create_db, init_schema
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.chat import ExchangeRequest
from app.schemas.conversation import ConversationDetail
from app.services.assistant_service import AssistantService
from app.services.ollama_service import ModelUsage
from app.services.research_service import SOURCE_SNIPPET_MAX_CHARS, source_references, to_sources
from app.services.web_extraction_service import PageDocument

CATEGORY_REF = {"type": "admin_entity", "entity": "category", "id": "a" * 24, "label": "Baby Shower"}


@pytest.fixture
def setup(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'refs.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    service = AssistantService(Settings(_env_file=None), object(), repo, research=None)  # type: ignore[arg-type]
    yield service, repo
    engine.dispose()


def test_exchange_references_are_saved_with_the_assistant_message(setup) -> None:
    service, repo = setup
    conv_id = asyncio.run(
        service.record_exchange("¿Qué categorías tengo?", "Tienes 1 categoría:", None, "user-alice", [CATEGORY_REF])
    )

    detail = ConversationDetail.model_validate(repo.get_with_messages(conv_id, "user-alice"))
    user, assistant = detail.messages
    assert user.references == []
    assert assistant.references == [CATEGORY_REF]


def test_messages_without_references_keep_an_empty_list(setup) -> None:
    service, repo = setup
    conv_id = asyncio.run(service.record_exchange("hola", "respuesta", None, "user-alice"))

    detail = ConversationDetail.model_validate(repo.get_with_messages(conv_id, "user-alice"))
    assert [m.references for m in detail.messages] == [[], []]


def test_init_schema_adds_the_references_column_to_an_old_database(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY, title VARCHAR(120), created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id VARCHAR(36), role VARCHAR(16), content TEXT, created_at DATETIME)"))
        conn.execute(text("INSERT INTO messages (conversation_id, role, content) VALUES ('c', 'assistant', 'antiguo')"))
    old.dispose()

    engine, _ = create_db(url)
    init_schema(engine)
    assert "references_json" in {c["name"] for c in inspect(engine).get_columns("messages")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT references_json FROM messages")).scalar() is None
    engine.dispose()


def test_exchange_request_limits_references() -> None:
    base = {"message": "hola", "answer": "respuesta"}
    assert ExchangeRequest(**base).references is None
    with pytest.raises(ValidationError):
        ExchangeRequest(**base, references=[CATEGORY_REF] * 61)
    with pytest.raises(ValidationError):
        ExchangeRequest(**base, references=[{"type": "admin_entity", "label": "x" * 25000}])


class StreamOllama:
    def __init__(self) -> None:
        self.sent: list = []

    async def chat_stream(self, messages, temperature, max_tokens, on_usage=None, think=False):
        self.sent.append(messages)
        yield "content", "Explicación"
        if on_usage is not None:
            on_usage(ModelUsage(prompt_tokens=1, completion_tokens=1))


def test_model_turn_saves_the_server_references_without_sending_them_to_the_model(tmp_path) -> None:
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'stream.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    ollama = StreamOllama()
    service = AssistantService(Settings(_env_file=None), ollama, repo, research=None)  # type: ignore[arg-type]

    async def run() -> list[tuple[str, dict]]:
        return [e async for e in service.stream_reply("¿por qué?", None, "user-alice", 0.7, 128, references=[CATEGORY_REF])]

    events = asyncio.run(run())
    done = events[-1]
    assert done[0] == "done" and done[1]["references"] == [CATEGORY_REF]
    assert "Baby Shower" not in str(ollama.sent)
    detail = ConversationDetail.model_validate(repo.get_with_messages(done[1]["conversation_id"], "user-alice"))
    assert detail.messages[1].references == [CATEGORY_REF]
    engine.dispose()


def test_web_sources_keep_the_provider_snippet_or_the_read_text() -> None:
    docs = [
        PageDocument("https://ollama.com/docs", "ollama.com", "Docs", None, "texto leído", snippet="Fragmento del buscador"),
        PageDocument("https://example.com/a", "example.com", "Guía", None, "x " * 400),
    ]
    sources = to_sources(docs, [1])
    refs = source_references(sources)

    assert refs[0] == {
        "type": "web_source",
        "index": 1,
        "title": "Docs",
        "url": "https://ollama.com/docs",
        "domain": "ollama.com",
        "snippet": "Fragmento del buscador",
        "cited": True,
    }
    assert refs[1]["cited"] is False
    assert refs[1]["snippet"].endswith("…")
    assert len(refs[1]["snippet"]) <= SOURCE_SNIPPET_MAX_CHARS
