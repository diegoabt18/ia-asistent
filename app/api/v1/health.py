import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request

from app.schemas.common import DependenciesResponse, DependencyStatus
from app.services.ollama_service import OllamaService
from app.services.search_service import SearchService

logger = logging.getLogger(__name__)

public_router = APIRouter(tags=["health"])
router = APIRouter(tags=["health"])


@public_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _check(name: str, check: Callable[[], Awaitable[object]]) -> DependencyStatus:
    try:
        await check()
        return DependencyStatus(name=name, status="ok")
    except Exception as exc:  # cada dependencia se reporta por separado, nunca tumba el endpoint
        logger.warning("Dependencia %s no disponible: %s", name, exc)
        return DependencyStatus(name=name, status="error", detail=exc.__class__.__name__)


@router.get("/health/dependencies", response_model=DependenciesResponse)
async def dependencies(request: Request) -> DependenciesResponse:
    state = request.app.state
    ollama = OllamaService(state.settings, state.http)
    search = SearchService(state.settings, state.http)

    checks = [
        await _check("redis", lambda: state.redis.ping()),
        await _check("ollama", lambda: ollama.list_models()),
        await _check("searxng", lambda: search.search("test", limit=1)),
    ]
    overall = "ok" if all(c.status == "ok" for c in checks) else "degraded"
    return DependenciesResponse(status=overall, services=checks)
