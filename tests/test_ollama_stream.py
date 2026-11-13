import json

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.ollama_service import OllamaService


def _ndjson(*chunks: dict) -> bytes:
    return ("\n".join(json.dumps(c) for c in chunks) + "\n").encode()


@pytest.mark.asyncio
async def test_chat_stream_yields_tokens_until_done() -> None:
    body = _ndjson(
        {"message": {"role": "assistant", "content": "Hola"}, "done": False},
        {"message": {"role": "assistant", "content": " mundo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["think"] is False
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = OllamaService(Settings(), client)
        pieces = [p async for p in service.chat_stream([{"role": "user", "content": "hi"}], 0.1, 32)]
    assert pieces == [("content", "Hola"), ("content", " mundo")]


@pytest.mark.asyncio
async def test_chat_stream_sends_think_when_requested() -> None:
    body = _ndjson({"message": {"role": "assistant", "content": "ok"}, "done": True})

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["think"] is True
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = OllamaService(Settings(), client)
        pieces = [
            p
            async for p in service.chat_stream(
                [{"role": "user", "content": "hi"}], 0.1, 32, think=True
            )
        ]
    assert pieces == [("content", "ok")]


@pytest.mark.asyncio
async def test_chat_stream_separates_thinking_from_content() -> None:
    body = _ndjson(
        {"message": {"role": "assistant", "thinking": "Voy a pensar"}, "done": False},
        {"message": {"role": "assistant", "content": "Respuesta"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = OllamaService(Settings(), client)
        pieces = [
            p
            async for p in service.chat_stream(
                [{"role": "user", "content": "hi"}], 0.1, 32, think=True
            )
        ]
    assert pieces == [("thinking", "Voy a pensar"), ("content", "Respuesta")]


@pytest.mark.asyncio
async def test_chat_stream_wraps_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = OllamaService(Settings(), client)
        with pytest.raises(UpstreamServiceError):
            async for _ in service.chat_stream([{"role": "user", "content": "hi"}], 0.1, 32):
                pass
