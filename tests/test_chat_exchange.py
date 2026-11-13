import asyncio

import pytest

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import create_db, init_schema
from app.core.exceptions import ConversationNotFoundError
from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import AssistantService


class NoModelOllama:
    """Si esta clase recibe una llamada, el endpoint de intercambio estaría usando el modelo."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args, **kwargs):  # pragma: no cover - no debe ejecutarse
        self.calls += 1
        raise AssertionError("record_exchange no debe llamar al modelo")

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover - no debe ejecutarse
        self.calls += 1
        raise AssertionError("record_exchange no debe llamar al modelo")
        yield  # pragma: no cover


@pytest.fixture
def setup(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'exchange.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    ollama = NoModelOllama()
    service = AssistantService(Settings(_env_file=None), ollama, repo, research=None)  # type: ignore[arg-type]
    yield service, repo, ollama
    engine.dispose()


def test_exchange_creates_conversation_without_calling_the_model(setup) -> None:
    service, repo, ollama = setup
    conv_id = asyncio.run(
        service.record_exchange("Últimos 10 pedidos", "No hay pedidos.", None, "user-alice")
    )

    assert repo.exists(conv_id, "user-alice")
    assert ollama.calls == 0


def test_exchange_continues_an_existing_conversation(setup) -> None:
    service, repo, _ = setup
    first = asyncio.run(service.record_exchange("uno", "respuesta 1", None, "user-alice"))
    second = asyncio.run(service.record_exchange("dos", "respuesta 2", first, "user-alice"))

    assert second == first
    assert repo.exists(first, "user-alice")


def test_exchange_rejects_another_users_conversation(setup) -> None:
    service, _, ollama = setup
    first = asyncio.run(service.record_exchange("secreto", "respuesta", None, "user-alice"))

    with pytest.raises(ConversationNotFoundError):
        asyncio.run(service.record_exchange("intruso", "x", first, "user-bob"))
    assert ollama.calls == 0
