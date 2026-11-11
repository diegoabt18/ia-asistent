from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_search_service
from app.schemas.search import SearchResponse
from app.services.search_service import SearchService

router = APIRouter(tags=["search"])


# Fase 1: búsqueda directa en SearXNG. La investigación completa (extracción y síntesis) llega en la Fase 3.
@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(default=10, ge=1, le=10),
    service: SearchService = Depends(get_search_service),
) -> SearchResponse:
    results = await service.search(q, limit=limit)
    return SearchResponse(query=q, results=results)
