import asyncio
from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import create_db, init_schema, utcnow
from app.core.exceptions import ConversationNotFoundError
from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import AssistantService
from app.services.ollama_service import ChatCompletion, ModelUsage


class FakeOllama:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, temperature, max_tokens, think=False) -> ChatCompletion:
        self.calls += 1
        return ChatCompletion(text="respuesta", usage=ModelUsage(prompt_tokens=11, completion_tokens=5))

    async def chat_stream(self, messages, temperature, max_tokens, on_usage=None, think=False):
        self.calls += 1
        yield "content", "res"
        yield "content", "puesta"
        if on_usage is not None:
            on_usage(ModelUsage(prompt_tokens=11, completion_tokens=5))


@pytest.fixture
def setup(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'svc.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    ollama = FakeOllama()
    service = AssistantService(Settings(_env_file=None), ollama, repo, research=None)  # type: ignore[arg-type]
    yield service, repo, ollama
    engine.dispose()


def test_reply_saves_turn_for_owner_and_records_usage(setup) -> None:
    service, repo, _ = setup
    result = asyncio.run(service.reply("hola", None, "user-alice", 0.7, 64))

    assert repo.exists(result.conversation_id, "user-alice")
    totals = repo.usage_totals("user-alice", utcnow() - timedelta(days=1))
    assert totals == {"requests": 1, "prompt_tokens": 11, "completion_tokens": 5}
    assert repo.usage_totals("user-bob", utcnow() - timedelta(days=1))["requests"] == 0


def test_reply_cannot_continue_another_users_conversation(setup) -> None:
    service, repo, ollama = setup
    first = asyncio.run(service.reply("secreto", None, "user-alice", 0.7, 64))

    with pytest.raises(ConversationNotFoundError):
        asyncio.run(service.reply("intruso", first.conversation_id, "user-bob", 0.7, 64))
    # No se llamó al modelo ni se registró consumo para el intruso.
    assert ollama.calls == 1
    assert repo.usage_totals("user-bob", utcnow() - timedelta(days=1))["requests"] == 0


def test_stream_records_usage_once_finished(setup) -> None:
    service, repo, _ = setup

    async def run() -> list[str]:
        events = []
        async for event, data in service.stream_reply("hola", None, "user-alice", 0.7, 64):
            events.append(event)
        return events

    events = asyncio.run(run())
    assert events[-1] == "done"
    assert repo.usage_totals("user-alice", utcnow() - timedelta(days=1)) == {
        "requests": 1,
        "prompt_tokens": 11,
        "completion_tokens": 5,
    }
