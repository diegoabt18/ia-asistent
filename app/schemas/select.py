from typing import Any

from pydantic import BaseModel, Field

NAME_PATTERN = r"^[a-z][a-z0-9_.]{1,59}$"


class SelectArg(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    # "str", "int", "bool" o una lista cerrada de valores permitidos.
    kind: str | list[str] = "str"
    required: bool = False
    description: str = Field(default="", max_length=200)


class SelectTool(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(min_length=1, max_length=200)
    args: list[SelectArg] = Field(default_factory=list, max_length=8)


class SelectExample(BaseModel):
    """Interpretación ya confirmada por el usuario (la recupera el Server, no el asistente)."""

    message: str = Field(min_length=1, max_length=300)
    tool: str = Field(pattern=NAME_PATTERN)
    args: dict[str, Any] = Field(default_factory=dict)


class SelectRequest(BaseModel):
    """Selección de herramienta (etapa 2). No crea conversación ni guarda historial.

    El catálogo y los módulos los envía el Server de Lumia, ya filtrados por los permisos del usuario.
    """

    message: str = Field(min_length=1, max_length=1000)
    # Una línea con la entidad activa ("pedido abierto: ORD-…"). La redacta el Server.
    context: str | None = Field(default=None, max_length=300)
    tools: list[SelectTool] = Field(min_length=1, max_length=40)
    modules: list[str] = Field(min_length=1, max_length=40)
    examples: list[SelectExample] = Field(default_factory=list, max_length=5)


class SelectIntent(BaseModel):
    tool: str
    args: dict[str, Any]


class SelectResponse(BaseModel):
    status: str
    intents: list[SelectIntent]
    need: str
    module: str
    # Problemas al leer la salida del modelo (vacío si todo fue bien). El Server decide qué hacer.
    errors: list[str]
    model: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
