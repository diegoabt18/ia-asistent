from fastapi import APIRouter, Depends

from app.core.dependencies import get_research_service
from app.schemas.research import ResearchRequest, ResearchResponse
from app.services.research_service import ResearchService

router = APIRouter(tags=["research"])


@router.post("/research", response_model=ResearchResponse)
async def research(
    payload: ResearchRequest,
    service: ResearchService = Depends(get_research_service),
) -> ResearchResponse:
    result = await service.answer(payload.question)
    return ResearchResponse(question=result.question, answer=result.answer, sources=result.sources)
