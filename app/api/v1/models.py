from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.dependencies import get_ollama_service
from app.schemas.common import ModelsResponse
from app.services.ollama_service import OllamaService

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelsResponse)
async def list_models(
    ollama: OllamaService = Depends(get_ollama_service),
    settings: Settings = Depends(get_settings),
) -> ModelsResponse:
    models = await ollama.list_models()
    return ModelsResponse(
        configured_model=settings.ollama_model,
        configured_available=settings.ollama_model in models,
        models=models,
    )
