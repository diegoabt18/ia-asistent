import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import app.models  # noqa: F401  registra los modelos en Base.metadata
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import create_db, init_schema
from app.core.exceptions import ConversationNotFoundError, ModelBusyError, UpstreamServiceError
from app.repositories.conversation_repository import ConversationRepository
from app.services.music.controller import MusicController
from app.services.redis_service import create_redis_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, session_factory = create_db(settings.database_url)
    init_schema(engine)
    removed = ConversationRepository(session_factory).purge_usage(settings.usage_retention_days)
    if removed:
        logging.getLogger(__name__).info("Registros de consumo antiguos eliminados: %d", removed)

    app.state.settings = settings
    app.state.http = httpx.AsyncClient()
    app.state.redis = create_redis_client(settings)
    app.state.session_factory = session_factory
    # Límite compartido por todas las peticiones: los servicios se crean por petición.
    app.state.verify_slots = asyncio.Semaphore(max(1, settings.verification_max_concurrent))
    app.state.model_slots = asyncio.Semaphore(max(1, settings.ollama_max_concurrent))
    # Módulo de música: no abre procesos ni conexiones hasta que se usa (el reproductor arranca con
    # la primera orden de reproducir).
    app.state.music = MusicController(settings)
    try:
        yield
    finally:
        await app.state.music.close()
        await app.state.http.aclose()
        await app.state.redis.aclose()
        engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    description="API central del asistente local. Usa Ollama, SearXNG y Redis existentes.",
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type"],
    )


@app.exception_handler(ModelBusyError)
async def model_busy_handler(request: Request, exc: ModelBusyError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "El modelo está ocupado. Inténtalo de nuevo en unos segundos."},
        headers={"Retry-After": "10"},
    )


@app.exception_handler(UpstreamServiceError)
async def upstream_error_handler(request: Request, exc: UpstreamServiceError) -> JSONResponse:
    # El detalle interno (URL, error de red) va al log, no a la respuesta.
    logging.getLogger(__name__).warning("Fallo en %s: %s", exc.service, exc.detail)
    return JSONResponse(
        status_code=502,
        content={"detail": f"Error en el servicio {exc.service}"},
    )


@app.exception_handler(ConversationNotFoundError)
async def conversation_not_found_handler(
    request: Request, exc: ConversationNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Conversación no encontrada"})


app.include_router(api_router, prefix="/api/v1")
