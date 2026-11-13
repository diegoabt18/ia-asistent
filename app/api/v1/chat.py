import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_assistant_service
from app.core.security import UserRef
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.assistant_service import AssistantService
from app.services.verification_service import to_schema

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    user_ref: UserRef,
    assistant: AssistantService = Depends(get_assistant_service),
) -> ChatResponse:
    result = await assistant.reply(
        payload.message,
        payload.conversation_id,
        user_ref,
        payload.temperature,
        payload.max_tokens,
        use_web=payload.use_web,
        context=payload.context,
        think=payload.think,
    )
    return ChatResponse(
        conversation_id=result.conversation_id,
        model=assistant.model,
        response=result.text,
        sources=result.sources,
        verification=to_schema(result.verification) if result.verification else None,
    )


async def _to_sse(events: AsyncIterator[tuple[str, dict]]) -> AsyncIterator[str]:
    async for event, data in events:
        yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream", response_class=StreamingResponse)
async def chat_stream(
    payload: ChatRequest,
    user_ref: UserRef,
    assistant: AssistantService = Depends(get_assistant_service),
) -> StreamingResponse:
    # La validación de la conversación ocurre antes de empezar el stream, para poder devolver 404.
    if payload.conversation_id is not None:
        await assistant.validate_conversation(payload.conversation_id, user_ref)
    events = assistant.stream_reply(
        payload.message,
        payload.conversation_id,
        user_ref,
        payload.temperature,
        payload.max_tokens,
        use_web=payload.use_web,
        context=payload.context,
        think=payload.think,
    )
    return StreamingResponse(
        _to_sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
