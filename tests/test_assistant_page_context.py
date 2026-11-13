import asyncio
from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import create_db, init_schema, utcnow
from app.prompts.system_prompts import SYSTEM_PROMPT
from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import CONTEXT_PREFIX, AssistantService, build_messages
from app.services.ollama_service import ChatCompletion, ModelUsage


class CapturingOllama:
    def __init__(self) -> None:
        self.sent: list[list[dict]] = []

    async def chat(self, messages, temperature, max_tokens, think=False) -> ChatCompletion:
        self.sent.append(messages)
        return ChatCompletion(text="ok", usage=ModelUsage(prompt_tokens=1, completion_tokens=1))

    async def chat_stream(self, messages, temperature, max_tokens, on_usage=None, think=False):
        self.sent.append(messages)
        yield "ok"
        if on_usage is not None:
            on_usage(ModelUsage(prompt_tokens=1, completion_tokens=1))


def test_context_is_a_separate_system_message_after_the_system_prompt() -> None:
    messages = build_messages([], "¿qué hago?", 1000, context="Producto: Vela")
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1] == {"role": "system", "content": CONTEXT_PREFIX + "Producto: Vela"}
    assert messages[-1] == {"role": "user", "content": "¿qué hago?"}


def test_no_context_keeps_the_original_shape() -> None:
    messages = build_messages([("user", "a"), ("assistant", "b")], "c", 1000)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]


@pytest.fixture
def service(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'ctx.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    ollama = CapturingOllama()
    svc = AssistantService(Settings(_env_file=None), ollama, repo, research=None)  # type: ignore[arg-type]
    yield svc, repo, ollama
    engine.dispose()


def test_context_reaches_the_model_but_is_not_stored(service) -> None:
    svc, repo, ollama = service
    result = asyncio.run(
        svc.reply("resume el producto", None, "user-alice", 0.7, 64, context="Producto: Vela de soja")
    )
    sent = ollama.sent[0]
    assert any(m["content"] == CONTEXT_PREFIX + "Producto: Vela de soja" for m in sent)

    stored = repo.get_with_messages(result.conversation_id, "user-alice")
    assert stored is not None
    assert [m.content for m in stored.messages] == ["resume el producto", "ok"]
    assert all("Vela de soja" not in m.content for m in stored.messages)


def test_context_is_also_used_by_the_stream_path(service) -> None:
    svc, _, ollama = service

    async def run() -> None:
        async for _ in svc.stream_reply("hola", None, "user-alice", 0.7, 64, context="Vista: Analítica"):
            pass

    asyncio.run(run())
    assert any(m["content"] == CONTEXT_PREFIX + "Vista: Analítica" for m in ollama.sent[0])
