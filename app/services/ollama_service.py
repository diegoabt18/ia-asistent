import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError


class OllamaService:
    """Adaptador HTTP para la instancia de Ollama existente."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def list_models(self) -> list[str]:
        try:
            r = await self._client.get(f"{self._settings.ollama_base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
        return [m["name"] for m in r.json().get("models", [])]

    async def chat(self, message: str, temperature: float, max_tokens: int) -> str:
        payload = {
            "model": self._settings.ollama_model,
            "messages": [{"role": "user", "content": message}],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            r = await self._client.post(
                f"{self._settings.ollama_base_url}/api/chat",
                json=payload,
                timeout=self._settings.ollama_timeout_seconds,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("Ollama", str(exc)) from exc
        return r.json()["message"]["content"]
