import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.config import Settings, get_settings
from app.core.database import create_db, init_schema, utcnow
from app.core.dependencies import get_selector_service
from app.main import app
from app.prompts.selector_prompt import build_system_prompt, output_schema, parse_output
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.select import SelectRequest
from app.services.ollama_service import ChatCompletion, ModelUsage
from app.services.selector_service import SelectorService

KEY = "clave-select-123"
HEADERS = {"X-API-Key": KEY, "X-User-Ref": "user-sel"}
TOOLS = [
    {"name": "orders.count", "description": "Contar pedidos", "args": [{"name": "status", "kind": ["paid", "pending"]}]},
    {"name": "products.search", "description": "Buscar productos por nombre", "args": [{"name": "query", "required": True}]},
]
MODULES = ["pedidos", "productos", "clientes"]


class StructuredOllama:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def structured(self, messages, schema, max_tokens, num_ctx) -> ChatCompletion:
        self.calls.append({"messages": messages, "schema": schema, "max_tokens": max_tokens, "num_ctx": num_ctx})
        return ChatCompletion(text=self.answer, usage=ModelUsage(prompt_tokens=900, completion_tokens=20))


@pytest.fixture
def make_service(tmp_path):
    engine, factory = create_db(f"sqlite:///{(tmp_path / 'sel.db').as_posix()}")
    init_schema(engine)
    repo = ConversationRepository(factory)

    def build(answer: str):
        ollama = StructuredOllama(answer)
        return SelectorService(Settings(_env_file=None), ollama, repo), ollama, repo  # type: ignore[arg-type]

    yield build
    engine.dispose()


def request(message: str = "¿cuántos pedidos pagados hay?", **extra) -> SelectRequest:
    return SelectRequest(message=message, tools=TOOLS, modules=MODULES, **extra)


def test_select_sends_the_catalog_and_a_closed_schema_and_records_usage(make_service) -> None:
    answer = json.dumps({"status": "tool", "intents": [{"tool": "orders.count", "args": {"status": "paid"}}], "need": "", "module": "pedidos"})
    svc, ollama, repo = make_service(answer)
    result = asyncio.run(svc.select("user-sel", request()))
    assert result.status == "tool" and result.intents[0].tool == "orders.count" and result.errors == []
    call = ollama.calls[0]
    assert "orders.count: Contar pedidos" in call["messages"][0]["content"]
    assert call["schema"]["properties"]["intents"]["items"]["properties"]["tool"]["enum"] == ["orders.count", "products.search"]
    assert call["schema"]["properties"]["module"]["enum"] == [*MODULES, "none"]
    # Sin historial: no crea conversación, pero el consumo se registra.
    assert repo.list_with_counts("user-sel", limit=10, offset=0) == []
    assert repo.usage_totals("user-sel", utcnow() - timedelta(days=1))["requests"] == 1


def test_missing_capability_keeps_need_and_module(make_service) -> None:
    answer = json.dumps({"status": "missing", "intents": [], "need": "reseñas negativas", "module": "clientes"})
    svc, _, _ = make_service(answer)
    result = asyncio.run(svc.select("user-sel", request("muéstrame las reseñas malas")))
    assert (result.status, result.need, result.module) == ("missing", "reseñas negativas", "clientes")


def test_bad_model_output_never_raises() -> None:
    for raw in ["", "no es json", "[]", json.dumps({"status": "x", "intents": [{"tool": "drop.db"}] * 3, "module": "otro"})]:
        selection, errors = parse_output(raw, ["orders.count"], MODULES)
        assert errors and selection["intents"] == [] and selection["module"] == "none"


def test_prompt_only_mentions_tools_and_examples_of_the_catalog_sent() -> None:
    prompt = build_system_prompt(TOOLS, MODULES)
    assert "products.stock" not in prompt and "reminders.create" not in prompt
    assert "products.search" in prompt and '"status":"missing"' in prompt
    # Mismo catálogo → mismo prompt (estático entre llamadas).
    assert prompt == build_system_prompt(TOOLS, MODULES)
    assert output_schema(["a.b"], ["x"])["required"] == ["status", "intents", "need", "module"]


def test_user_examples_and_context_go_in_the_user_message(make_service) -> None:
    svc, ollama, _ = make_service(json.dumps({"status": "chat", "intents": [], "need": "", "module": "none"}))
    asyncio.run(svc.select("user-sel", request("novedades", context="pedido abierto: ORD-1", examples=[{"message": "novedades", "tool": "orders.count", "args": {}}])))
    user = ollama.calls[0]["messages"][1]["content"]
    assert "Frases que este usuario ya confirmó" in user and "Contexto: pedido abierto: ORD-1" in user


@pytest.fixture
def http_client():
    class FakeSelector:
        async def select(self, user_ref, payload):
            from app.schemas.select import SelectResponse

            return SelectResponse(status="chat", intents=[], need="", module="none", errors=[], model="q", latency_ms=1.0, prompt_tokens=1, completion_tokens=1)

    app.dependency_overrides[get_settings] = lambda: Settings(api_key=KEY)
    app.dependency_overrides[get_selector_service] = lambda: FakeSelector()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_select_endpoint_requires_key_user_and_bounds_input(http_client) -> None:
    body = {"message": "hola", "tools": TOOLS, "modules": MODULES}
    assert http_client.post("/api/v1/select", json=body, headers={"X-API-Key": KEY}).status_code == 422
    assert http_client.post("/api/v1/select", json=body, headers={"X-User-Ref": "u"}).status_code in (401, 403)
    assert http_client.post("/api/v1/select", json={**body, "tools": []}, headers=HEADERS).status_code == 422
    assert http_client.post("/api/v1/select", json={**body, "message": "x" * 1001}, headers=HEADERS).status_code == 422
    ok = http_client.post("/api/v1/select", json=body, headers=HEADERS)
    assert ok.status_code == 200 and ok.json()["status"] == "chat"
