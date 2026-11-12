import asyncio

from fastapi import APIRouter, Depends

from app.core.dependencies import get_assistant_service
from app.core.security import UserRef
from app.schemas.generate import GenerateRequest, GenerateResponse
from app.services.assistant_service import AssistantService

router = APIRouter(tags=["generate"])


@router.post("/generate", response_model=GenerateResponse)
async def generate(
    payload: GenerateRequest,
    user_ref: UserRef,
    assistant: AssistantService = Depends(get_assistant_service),
) -> GenerateResponse:
    result = await assistant.generate(
        user_ref,
        payload.instructions,
        payload.text,
        payload.temperature,
        payload.max_tokens,
    )
    return GenerateResponse(model=result.model, text=result.text)
