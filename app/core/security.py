import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings

# Identificador opaco del usuario que envía el servidor (el _id de identity_db).
USER_REF_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

# Cabecera obligatoria en las rutas que consumen el modelo o acceden a conversaciones.
# Solo el servidor (con la API key de servicio) puede enviarla; la API no la verifica por sí misma.
UserRef = Annotated[str, Header(alias="X-User-Ref", pattern=USER_REF_PATTERN)]


def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY no configurada en el servidor",
        )
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida",
        )
