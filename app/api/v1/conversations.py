import asyncio

from fastapi import APIRouter, Depends, Query, Response

from app.core.dependencies import get_conversation_repository
from app.core.exceptions import ConversationNotFoundError
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.conversation import ConversationDetail, ConversationSummary

router = APIRouter(tags=["conversations"])


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    repo: ConversationRepository = Depends(get_conversation_repository),
) -> list[ConversationSummary]:
    rows = await asyncio.to_thread(repo.list_with_counts, limit, offset)
    return [
        ConversationSummary(
            id=conv.id,
            title=conv.title,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            message_count=count,
        )
        for conv, count in rows
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    repo: ConversationRepository = Depends(get_conversation_repository),
) -> ConversationDetail:
    conv = await asyncio.to_thread(repo.get_with_messages, conversation_id)
    if conv is None:
        raise ConversationNotFoundError(conversation_id)
    return ConversationDetail.model_validate(conv)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    repo: ConversationRepository = Depends(get_conversation_repository),
) -> Response:
    deleted = await asyncio.to_thread(repo.delete, conversation_id)
    if not deleted:
        raise ConversationNotFoundError(conversation_id)
    return Response(status_code=204)
