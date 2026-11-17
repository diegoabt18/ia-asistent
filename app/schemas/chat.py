import json
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field

from app.schemas.research import ResearchSource
from app.schemas.verification import VerificationOut

REFERENCES_MAX_ITEMS = 60
REFERENCES_MAX_CHARS = 20000


def _check_references_size(value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if value is not None and len(json.dumps(value, ensure_ascii=False)) > REFERENCES_MAX_CHARS:
        raise ValueError("references too large")
    return value


# Referencias estructuradas de un mensaje. Las arma y valida el servidor de Lumia; aquí solo se
# acotan (cantidad y tamaño) y se guardan con la respuesta. Nunca van al prompt del modelo.
MessageReferences = Annotated[
    list[dict[str, Any]] | None,
    Field(default=None, max_length=REFERENCES_MAX_ITEMS),
    AfterValidator(_check_references_size),
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    # Sin conversation_id se crea una conversación nueva.
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    # Si es true, busca en la web antes de responder y devuelve las fuentes.
    use_web: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)
    # Activa el razonamiento del modelo. Consume tokens de max_tokens y tarda más.
    think: bool = False
    # Datos de solo lectura de la pantalla del administrador. Va al prompt y NO se guarda en el historial.
    context: str | None = Field(default=None, max_length=2000)
    # Qué puede hacer el sistema por este usuario. Lo escribe el servidor de Lumia (nunca el navegador)
    # y va en el prompt de sistema. No se guarda en el historial.
    capabilities: str | None = Field(default=None, max_length=2000)
    # Referencias de los datos que el servidor leyó para este turno. En búsqueda web se guardan las
    # fuentes en su lugar.
    references: MessageReferences = None


class ChatResponse(BaseModel):
    conversation_id: str
    model: str
    response: str
    sources: list[ResearchSource] = Field(default_factory=list)
    # Solo se rellena cuando use_web es true.
    verification: VerificationOut | None = None
    # Referencias guardadas con la respuesta (fuentes web o las que envió el servidor).
    references: list[dict[str, Any]] = Field(default_factory=list)


class ExchangeRequest(BaseModel):
    """Turno ya resuelto sin modelo (herramienta del backend). Solo se guarda en el historial."""

    message: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    # Referencias del turno (registros del panel que devolvió la herramienta).
    references: MessageReferences = None


class ExchangeResponse(BaseModel):
    conversation_id: str
