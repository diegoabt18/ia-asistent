import asyncio

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.dependencies import get_conversation_repository, get_research_service
from app.core.security import UserRef
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.research import ResearchRequest, ResearchResponse
from app.services.research_service import ResearchService
from app.services.verification_service import to_schema

router = APIRouter(tags=["research"])


@router.post("/research", response_model=ResearchResponse)
async def research(
    payload: ResearchRequest,
    user_ref: UserRef,
    service: ResearchService = Depends(get_research_service),
    repo: ConversationRepository = Depends(get_conversation_repository),
    settings: Settings = Depends(get_settings),
) -> ResearchResponse:
    result = await service.answer(payload.question)
    if result.usage is not None:
        await asyncio.to_thread(
            repo.record_usage, user_ref, "research", settings.ollama_model, result.usage
        )
    return ResearchResponse(
        question=result.question,
        answer=result.answer,
        queries=result.queries,
        verification=to_schema(result.verification),
        sources=result.sources,
    )
