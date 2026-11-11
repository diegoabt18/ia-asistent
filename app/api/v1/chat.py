from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.dependencies import get_ollama_service
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.ollama_service import OllamaService

router = APIRouter(tags=["chat"])


# Fase 1: respuesta sin historial de conversación. El historial llega en la Fase 2.
@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    ollama: OllamaService = Depends(get_ollama_service),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    text = await ollama.chat(payload.message, payload.temperature, payload.max_tokens)
    return ChatResponse(model=settings.ollama_model, response=text)
