from fastapi import APIRouter, Depends

from app.core.dependencies import get_selector_service
from app.core.security import UserRef
from app.schemas.select import SelectRequest, SelectResponse
from app.services.selector_service import SelectorService

router = APIRouter(tags=["select"])


@router.post("/select", response_model=SelectResponse)
async def select(
    payload: SelectRequest,
    user_ref: UserRef,
    selector: SelectorService = Depends(get_selector_service),
) -> SelectResponse:
    # Etapa 2 de Lumi: propone una herramienta del catálogo enviado. No ejecuta nada ni guarda historial.
    return await selector.select(user_ref, payload)
