import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError


@dataclass(frozen=True)
class ModelUsage:
    """Tokens consumidos por una llamada al modelo, tal como los informa Ollama."""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class ChatCompletion:
    text: str
    usage: ModelUsage


def _usage_from(chunk: dict) -> ModelUsage:
    # Ollama informa los contadores en la respuesta final. Si faltan, se cuentan como 0.
    return ModelUsage(
        prompt_tokens=int(chunk.get("prompt_eval_count") or 0),
        completion_tokens=int(chunk.get("eval_count") or 0),
    )


class OllamaService:
    """Adaptador HTTP para la instancia de Ollama existente."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _payload(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        stream: bool,
        think: bool,
    ) -> dict:
        return {
            "model": self._settings.ollama_model,
            "messages": messages,
            "stream": stream,
            # Con think=False, los modelos con razonamiento (p. ej. qwen3.5) no gastan tokens en el
            # "thinking": la respuesta va en `content` y no sale en blanco.
            "think": think,
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
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        think: bool = False,
    ) -> ChatCompletion:
        payload = self._payload(messages, temperature, max_tokens, stream=False, think=think)
        try:
            r = await self._client.post(
                self._chat_url, json=payload, timeout=self._settings.ollama_timeout_seconds
            )
            r.raise_for_status()
            body = r.json()
            text = body["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
        return ChatCompletion(text=text, usage=_usage_from(body))

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        on_usage: Callable[[ModelUsage], None] | None = None,
        think: bool = False,
    ) -> AsyncIterator[tuple[str, str]]:
        """Emite fragmentos a medida que Ollama los genera (NDJSON).

        Cada fragmento es `("thinking", texto)` o `("content", texto)`. El razonamiento solo llega
        con `think=True`; consume tokens de `max_tokens`.
        `on_usage` se invoca una vez, con los tokens consumidos, al terminar el stream.
        """
        payload = self._payload(messages, temperature, max_tokens, stream=True, think=think)
        try:
            async with self._client.stream(
                "POST", self._chat_url, json=payload, timeout=self._settings.ollama_timeout_seconds
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message", {})
                    thinking = message.get("thinking", "")
                    if thinking:
                        yield "thinking", thinking
                    text = message.get("content", "")
                    if text:
                        yield "content", text
                    if chunk.get("done"):
                        if on_usage is not None:
                            on_usage(_usage_from(chunk))
                        return
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
