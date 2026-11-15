import asyncio

import pytest
from pydantic import ValidationError

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import create_db, init_schema
from app.prompts.system_prompts import CAPABILITIES_PREFIX, SYSTEM_PROMPT, chat_system_prompt
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.chat import ChatRequest
from app.services.assistant_service import AssistantService
from tests.test_assistant_page_context import CapturingOllama

CAPS = "Hoy es sábado, 14 de noviembre de 2026.\n- Recordatorios con fecha y hora."


def test_base_prompt_forbids_claiming_actions() -> None:
    assert "nunca digas que creaste, programaste" in SYSTEM_PROMPT
    assert chat_system_prompt(None) == SYSTEM_PROMPT
    assert chat_system_prompt("") == SYSTEM_PROMPT


def test_capabilities_are_appended_to_the_system_prompt() -> None:
    prompt = chat_system_prompt(CAPS)
    assert prompt.startswith(SYSTEM_PROMPT)
    assert prompt.endswith(CAPABILITIES_PREFIX + CAPS)


def test_capabilities_have_a_size_limit() -> None:
    assert ChatRequest(message="hola", capabilities=CAPS).capabilities == CAPS
    with pytest.raises(ValidationError):
        ChatRequest(message="hola", capabilities="x" * 2001)


@pytest.fixture
def service(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'caps.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    ollama = CapturingOllama()
    svc = AssistantService(Settings(_env_file=None), ollama, repo, research=None)  # type: ignore[arg-type]
    yield svc, repo, ollama
    engine.dispose()


def test_capabilities_reach_the_model_in_both_paths_and_are_not_stored(service) -> None:
    svc, repo, ollama = service
    result = asyncio.run(svc.reply("ponme una alarma", None, "user-alice", 0.7, 64, capabilities=CAPS))

    async def stream() -> None:
        async for _ in svc.stream_reply("hola", None, "user-alice", 0.7, 64, capabilities=CAPS):
            pass

    asyncio.run(stream())
    for sent in ollama.sent:
        assert sent[0] == {"role": "system", "content": chat_system_prompt(CAPS)}
    stored = repo.get_with_messages(result.conversation_id, "user-alice")
    assert stored is not None
    assert all("Recordatorios con fecha" not in m.content for m in stored.messages)
