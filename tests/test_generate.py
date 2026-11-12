import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.config import Settings, get_settings
from app.core.database import create_db, init_schema, utcnow
from app.core.dependencies import get_assistant_service
from app.main import app
from app.repositories.conversation_repository import ConversationRepository
from app.services.assistant_service import AssistantService
from app.services.ollama_service import ChatCompletion, ModelUsage

KEY = "clave-generate-123"
HEADERS = {"X-API-Key": KEY, "X-User-Ref": "user-gen"}


class CapturingOllama:
    def __init__(self) -> None:
        self.sent: list[list[dict]] = []

    async def chat(self, messages, temperature, max_tokens) -> ChatCompletion:
        self.sent.append(messages)
        return ChatCompletion(text="Vela de soja artesanal.", usage=ModelUsage(prompt_tokens=30, completion_tokens=6))

    async def chat_stream(self, messages, temperature, max_tokens, on_usage=None):
        raise AssertionError("generate no usa streaming")
        yield  # pragma: no cover


@pytest.fixture
def service(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'gen.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    ollama = CapturingOllama()
    svc = AssistantService(Settings(_env_file=None), ollama, repo, research=None)  # type: ignore[arg-type]
    yield svc, repo, ollama
    engine.dispose()


def test_generate_sends_instructions_as_system_and_text_as_user(service) -> None:
    svc, _, ollama = service
    result = asyncio.run(svc.generate("user-gen", "Eres un redactor.", "vela de soja", 0.4, 100))
    assert result.text == "Vela de soja artesanal."
    assert ollama.sent[0] == [
        {"role": "system", "content": "Eres un redactor."},
        {"role": "user", "content": "vela de soja"},
    ]


def test_generate_creates_no_conversation_and_records_usage(service) -> None:
    svc, repo, _ = service
    asyncio.run(svc.generate("user-gen", "Eres un redactor.", "vela", 0.4, 100))
    assert repo.list_with_counts("user-gen", limit=10, offset=0) == []
    totals = repo.usage_totals("user-gen", utcnow() - timedelta(days=1))
    assert totals == {"requests": 1, "prompt_tokens": 30, "completion_tokens": 6}


@pytest.fixture
def http_client(tmp_path):
    class FakeAssistant:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def generate(self, user_ref, instructions, text, temperature, max_tokens):
            self.calls.append((user_ref, instructions, text, temperature, max_tokens))

            class Result:
                model = "qwen-test"
                text = "Texto generado"

            return Result()

    fake = FakeAssistant()
    app.dependency_overrides[get_settings] = lambda: Settings(api_key=KEY)
    app.dependency_overrides[get_assistant_service] = lambda: fake
    yield TestClient(app), fake
    app.dependency_overrides.clear()


def test_generate_endpoint_requires_user_reference(http_client) -> None:
    client, _ = http_client
    response = client.post("/api/v1/generate", json={"instructions": "x", "text": "y"}, headers={"X-API-Key": KEY})
    assert response.status_code == 422


def test_generate_endpoint_returns_text_and_bounds_input(http_client) -> None:
    client, fake = http_client
    ok = client.post("/api/v1/generate", json={"instructions": "x", "text": "y"}, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json() == {"model": "qwen-test", "text": "Texto generado"}
    assert fake.calls[0][0] == "user-gen"

    too_long = client.post("/api/v1/generate", json={"instructions": "x", "text": "y" * 8001}, headers=HEADERS)
    assert too_long.status_code == 422
    assert client.post("/api/v1/generate", json={"instructions": "x", "text": "y", "max_tokens": 5000}, headers=HEADERS).status_code == 422
