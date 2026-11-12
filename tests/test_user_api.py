import uuid

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.config import Settings, get_settings
from app.core.database import create_db, init_schema
from app.core.dependencies import get_conversation_repository
from app.main import app
from app.repositories.conversation_repository import ConversationRepository
from app.services.ollama_service import ModelUsage

KEY = "clave-de-prueba"
ALICE = {"X-API-Key": KEY, "X-User-Ref": "user-alice"}
BOB = {"X-API-Key": KEY, "X-User-Ref": "user-bob"}


@pytest.fixture
def client(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)
    app.dependency_overrides[get_settings] = lambda: Settings(api_key=KEY)
    app.dependency_overrides[get_conversation_repository] = lambda: repo
    yield TestClient(app), repo
    app.dependency_overrides.clear()
    engine.dispose()


def test_user_ref_header_is_required_on_conversation_routes(client) -> None:
    http, _ = client
    response = http.get("/api/v1/conversations", headers={"X-API-Key": KEY})
    assert response.status_code == 422


def test_user_ref_header_rejects_unsafe_values(client) -> None:
    http, _ = client
    response = http.get(
        "/api/v1/conversations",
        headers={"X-API-Key": KEY, "X-User-Ref": "../../otro"},
    )
    assert response.status_code == 422


def test_conversation_routes_only_expose_the_callers_conversations(client) -> None:
    http, repo = client
    conv_id = str(uuid.uuid4())
    repo.add_exchange(conv_id, "user-alice", "hola", "¡hola!", "hola")

    assert len(http.get("/api/v1/conversations", headers=ALICE).json()) == 1
    assert http.get("/api/v1/conversations", headers=BOB).json() == []
    assert http.get(f"/api/v1/conversations/{conv_id}", headers=BOB).status_code == 404
    assert http.delete(f"/api/v1/conversations/{conv_id}", headers=BOB).status_code == 404
    assert http.get(f"/api/v1/conversations/{conv_id}", headers=ALICE).status_code == 200


def test_usage_endpoint_reports_only_the_callers_consumption(client) -> None:
    http, repo = client
    repo.record_usage("user-alice", "chat", "qwen", ModelUsage(prompt_tokens=30, completion_tokens=12))

    alice = http.get("/api/v1/usage", headers=ALICE).json()
    assert alice == {"days": 30, "requests": 1, "prompt_tokens": 30, "completion_tokens": 12}
    bob = http.get("/api/v1/usage", headers=BOB).json()
    assert bob["requests"] == 0


def test_usage_endpoint_validates_window(client) -> None:
    http, _ = client
    assert http.get("/api/v1/usage", headers=ALICE, params={"days": 0}).status_code == 422
    assert http.get("/api/v1/usage", headers=ALICE, params={"days": 91}).status_code == 422
