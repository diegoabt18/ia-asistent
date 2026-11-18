from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.select import NAME_PATTERN, SelectArg
from app.services.music.types import VIDEO_ID_PATTERN


class TrackOut(BaseModel):
    video_id: str
    title: str
    artist: str | None = None
    duration_seconds: int | None = None


class DeviceOut(BaseModel):
    connected: bool
    device_id: str | None = None
    bluetooth_connected: bool = False
    bluetooth_sink: str | None = None
    wifi_rssi: int | None = None
    buffer_ms: int | None = None
    free_heap: int | None = None
    firmware: str | None = None
    last_seen_seconds: float | None = None
    reconnects: int = 0


class MusicStateOut(BaseModel):
    status: Literal["idle", "playing", "paused"]
    # Cambia con cada pista nueva o al detener: el ESP32 reconecta el stream cuando cambia.
    generation: int
    current: TrackOut | None
    upcoming: list[TrackOut]
    streaming: bool
    device: DeviceOut


class MusicSearchResponse(BaseModel):
    tracks: list[TrackOut]


class MusicToolSpec(BaseModel):
    """Herramienta de música con la forma de `SelectTool` (para /select) más su módulo y permiso.

    El servidor de Lumia decide si la incluye en el catálogo según los permisos del usuario.
    """

    name: str
    description: str
    args: list[SelectArg]
    module: str
    permission: Literal["music.read", "music.control"]


class MusicCommandRequest(BaseModel):
    tool: str = Field(pattern=NAME_PATTERN)
    args: dict[str, Any] = Field(default_factory=dict)


class MusicCommandResponse(BaseModel):
    tool: str
    ok: bool
    # Texto determinista para el usuario (sin modelo).
    text: str
    # Código del fallo cuando ok es false: not_found, nothing_playing, queue_full, upstream, forbidden.
    error: str | None = None
    tracks: list[TrackOut] = Field(default_factory=list)
    state: MusicStateOut


class QueueAddRequest(BaseModel):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    video_id: str | None = Field(default=None, pattern=VIDEO_ID_PATTERN)

    @model_validator(mode="after")
    def _one_target(self) -> "QueueAddRequest":
        if (self.query is None) == (self.video_id is None):
            raise ValueError("indica query o video_id (solo uno)")
        return self


class HeartbeatRequest(BaseModel):
    bluetooth_connected: bool = False
    bluetooth_sink: str | None = Field(default=None, max_length=64)
    wifi_rssi: int | None = Field(default=None, ge=-127, le=0)
    buffer_ms: int | None = Field(default=None, ge=0, le=600_000)
    free_heap: int | None = Field(default=None, ge=0)
    firmware: str | None = Field(default=None, max_length=32)


class DeviceControlRequest(BaseModel):
    """Botones físicos del ESP32."""

    action: Literal["pause", "resume", "toggle", "next", "previous", "stop"]


class DevicePingResponse(BaseModel):
    ok: bool
    device_id: str
    generation: int
    status: Literal["idle", "playing", "paused"]
