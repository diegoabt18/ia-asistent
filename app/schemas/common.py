from typing import Literal

from pydantic import BaseModel


class DependencyStatus(BaseModel):
    name: str
    status: Literal["ok", "error"]
    detail: str | None = None


class DependenciesResponse(BaseModel):
    status: Literal["ok", "degraded"]
    services: list[DependencyStatus]


class ModelsResponse(BaseModel):
    configured_model: str
    configured_available: bool
    models: list[str]
