from fastapi import APIRouter, Depends

from app.api.v1 import chat, conversations, generate, health, models, research, search, usage
from app.core.security import require_api_key

api_router = APIRouter()
api_router.include_router(health.public_router)

# Todo lo demás requiere la cabecera X-API-Key. Las rutas de usuario además exigen X-User-Ref.
protected = APIRouter(dependencies=[Depends(require_api_key)])
protected.include_router(health.router)
protected.include_router(models.router)
protected.include_router(chat.router)
protected.include_router(conversations.router)
protected.include_router(search.router)
protected.include_router(research.router)
protected.include_router(usage.router)
protected.include_router(generate.router)
api_router.include_router(protected)
