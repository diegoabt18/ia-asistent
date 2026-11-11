import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError


class OllamaService:
    """Adaptador HTTP para la instancia de Ollama existente."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _payload(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int, stream: bool
    ) -> dict:
        return {
            "model": self._settings.ollama_model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

    @property
    def _chat_url(self) -> str:
        return f"{self._settings.ollama_base_url}/api/chat"

    async def list_models(self) -> list[str]:
        try:
            r = await self._client.get(f"{self._settings.ollama_base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
        return [m["name"] for m in r.json().get("models", [])]

    async def chat(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int
    ) -> str:
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        try:
            r = await self._client.post(
                self._chat_url, json=payload, timeout=self._settings.ollama_timeout_seconds
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
        return r.json()["message"]["content"]

    async def chat_stream(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int
    ) -> AsyncIterator[str]:
        """Emite fragmentos de texto a medida que Ollama los genera (NDJSON)."""
        payload = self._payload(messages, temperature, max_tokens, stream=True)
        try:
            async with self._client.stream(
                "POST", self._chat_url, json=payload, timeout=self._settings.ollama_timeout_seconds
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        yield text
                    if chunk.get("done"):
                        return
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
