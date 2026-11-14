import asyncio
import json

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ModelBusyError, UpstreamServiceError
from app.services.ollama_service import OllamaService


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _final_chunk() -> dict:
    return {"message": {"content": "hola"}, "done": True, "prompt_eval_count": 3, "eval_count": 2}


def test_calls_beyond_the_limit_wait_for_a_free_slot():
    async def run():
        active = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return httpx.Response(200, json=_final_chunk())

        slots = asyncio.Semaphore(2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = OllamaService(_settings(), client, slots)
            results = await asyncio.gather(
                *[service.chat([{"role": "user", "content": "x"}], 0.1, 10) for _ in range(5)]
            )
        assert [r.text for r in results] == ["hola"] * 5
        assert peak == 2

    asyncio.run(run())


def test_busy_model_raises_after_queue_timeout():
    async def run():
        slots = asyncio.Semaphore(1)
        await slots.acquire()  # plaza ocupada por otra petición
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_final_chunk()))
        ) as client:
            service = OllamaService(_settings(ollama_queue_timeout_seconds=0.05), client, slots)
            with pytest.raises(ModelBusyError):
                await service.chat([{"role": "user", "content": "x"}], 0.1, 10)

    asyncio.run(run())


def test_slot_is_released_after_an_upstream_error():
    async def run():
        slots = asyncio.Semaphore(1)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))
        ) as client:
            service = OllamaService(_settings(), client, slots)
            with pytest.raises(UpstreamServiceError):
                await service.chat([{"role": "user", "content": "x"}], 0.1, 10)
        assert not slots.locked()

    asyncio.run(run())


def test_stream_releases_the_slot_when_the_consumer_stops_early():
    async def run():
        lines = "\n".join(
            json.dumps(c)
            for c in [
                {"message": {"content": "a"}, "done": False},
                {"message": {"content": "b"}, "done": False},
                _final_chunk(),
            ]
        )
        slots = asyncio.Semaphore(1)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=lines))
        ) as client:
            service = OllamaService(_settings(), client, slots)
            stream = service.chat_stream([{"role": "user", "content": "x"}], 0.1, 10)
            first = await stream.__anext__()
            assert first == ("content", "a")
            assert slots.locked()
            await stream.aclose()  # el cliente se desconecta
        assert not slots.locked()

    asyncio.run(run())
