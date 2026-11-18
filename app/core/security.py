import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings

# Identificador opaco del usuario que envía el servidor (el _id de identity_db).
USER_REF_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

# Cabecera obligatoria en las rutas que consumen el modelo o acceden a conversaciones.
# Solo el servidor (con la API key de servicio) puede enviarla; la API no la verifica por sí misma.
UserRef = Annotated[str, Header(alias="X-User-Ref", pattern=USER_REF_PATTERN)]


DEVICE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

# Identificador del ESP32 (lo elige su firmware). Solo sirve para el estado y los logs, no autentica.
DeviceId = Annotated[str, Header(alias="X-Device-Id", pattern=DEVICE_ID_PATTERN)]


def require_music_enabled(settings: Settings = Depends(get_settings)) -> None:
    # Con el módulo desactivado, las rutas de música no existen a efectos del cliente.
    if not settings.music_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def require_device_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Autenticación del ESP32: `Authorization: Bearer <MUSIC_DEVICE_TOKEN>`.

    Es un secreto distinto de API_KEY: si el dispositivo se pierde o se extrae su firmware, solo
    expone el control de la música, no el modelo ni las conversaciones.
    """
    if not settings.music_device_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MUSIC_DEVICE_TOKEN no configurado en el servidor",
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode(), settings.music_device_token.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de dispositivo inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )


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
