import json

import httpx
import pytest

from app.core.config import Settings
from app.services.ollama_service import ModelUsage, OllamaService


@pytest.mark.asyncio
async def test_chat_returns_text_and_token_usage() -> None:
    body = {
        "message": {"role": "assistant", "content": "Hola"},
        "done": True,
        "prompt_eval_count": 42,
        "eval_count": 7,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        completion = await OllamaService(Settings(), client).chat(
            [{"role": "user", "content": "hi"}], 0.1, 32
        )
    assert completion.text == "Hola"
    assert completion.usage == ModelUsage(prompt_tokens=42, completion_tokens=7)


@pytest.mark.asyncio
async def test_missing_counters_count_as_zero() -> None:
    body = {"message": {"role": "assistant", "content": "ok"}, "done": True}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        completion = await OllamaService(Settings(), client).chat(
            [{"role": "user", "content": "hi"}], 0.1, 32
        )
    assert completion.usage == ModelUsage(prompt_tokens=0, completion_tokens=0)


@pytest.mark.asyncio
async def test_chat_stream_reports_usage_on_the_final_chunk() -> None:
    lines = [
        {"message": {"role": "assistant", "content": "Hola"}, "done": False},
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 10,
            "eval_count": 3,
        },
    ]
    body = ("\n".join(json.dumps(c) for c in lines) + "\n").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    seen: list[ModelUsage] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pieces = [
            p
            async for p in OllamaService(Settings(), client).chat_stream(
                [{"role": "user", "content": "hi"}], 0.1, 32, on_usage=seen.append
            )
        ]
    assert pieces == ["Hola"]
    assert seen == [ModelUsage(prompt_tokens=10, completion_tokens=3)]
