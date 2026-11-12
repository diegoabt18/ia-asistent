from pydantic import BaseModel, Field

from app.schemas.research import ResearchSource
from app.schemas.verification import VerificationOut


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    # Sin conversation_id se crea una conversación nueva.
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    # Si es true, busca en la web antes de responder y devuelve las fuentes.
    use_web: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)


class ChatResponse(BaseModel):
    conversation_id: str
    model: str
    response: str
    sources: list[ResearchSource] = Field(default_factory=list)
    # Solo se rellena cuando use_web es true.
    verification: VerificationOut | None = None
