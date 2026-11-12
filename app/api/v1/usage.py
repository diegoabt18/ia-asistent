import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_conversation_repository
from app.core.security import UserRef
from app.core.database import utcnow
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.usage import UsageSummary

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageSummary)
async def usage_summary(
    user_ref: UserRef,
    days: int = Query(default=30, ge=1, le=90),
    repo: ConversationRepository = Depends(get_conversation_repository),
) -> UsageSummary:
    """Consumo de inferencia del usuario indicado en los últimos `days` días."""
    since = utcnow() - timedelta(days=days)
    totals = await asyncio.to_thread(repo.usage_totals, user_ref, since)
    return UsageSummary(days=days, **totals)
