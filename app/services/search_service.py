import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.schemas.search import SearchResult


class SearchService:
    """Cliente HTTP para la instancia de SearXNG existente (requiere format=json habilitado)."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        try:
            r = await self._client.get(
                f"{self._settings.searxng_base_url}/search",
                params={"q": query, "format": "json"},
                timeout=self._settings.searxng_timeout_seconds,
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("SearXNG", str(exc)) from exc
        except ValueError as exc:
            raise UpstreamServiceError(
                "SearXNG", "respuesta no JSON; revisa que el formato json esté habilitado"
            ) from exc

        seen: set[str] = set()
        results: list[SearchResult] = []
        for item in data.get("results", []):
            url = item.get("url")
            title = item.get("title")
            if not url or not title or url in seen:
                continue
            seen.add(url)
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=item.get("content", ""),
                    engine=item.get("engine"),
                )
            )
            if len(results) >= limit:
                break
        return results
