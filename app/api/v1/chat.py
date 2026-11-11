import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_assistant_service
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.assistant_service import AssistantService

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    assistant: AssistantService = Depends(get_assistant_service),
) -> ChatResponse:
    conversation_id, text = await assistant.reply(
        payload.message, payload.conversation_id, payload.temperature, payload.max_tokens
    )
    return ChatResponse(conversation_id=conversation_id, model=assistant.model, response=text)


async def _to_sse(events: AsyncIterator[tuple[str, dict]]) -> AsyncIterator[str]:
    async for event, data in events:
        yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream", response_class=StreamingResponse)
async def chat_stream(
    payload: ChatRequest,
    assistant: AssistantService = Depends(get_assistant_service),
) -> StreamingResponse:
    # La validación de la conversación ocurre antes de empezar el stream, para poder devolver 404.
    if payload.conversation_id is not None:
        await assistant.validate_conversation(payload.conversation_id)
    events = assistant.stream_reply(
        payload.message, payload.conversation_id, payload.temperature, payload.max_tokens
    )
    return StreamingResponse(
        _to_sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
