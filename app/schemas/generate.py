from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    # Instrucciones fijas que define el servidor de Lumia. Van como mensaje de sistema.
    instructions: str = Field(min_length=1, max_length=2000)
    # Texto de trabajo (datos del producto o del formulario). Va como mensaje de usuario.
    text: str = Field(min_length=1, max_length=8000)
    temperature: float = Field(default=0.4, ge=0.0, le=1.0)
    max_tokens: int = Field(default=400, ge=1, le=1024)


class GenerateResponse(BaseModel):
    model: str
    text: str
