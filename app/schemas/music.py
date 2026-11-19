from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.select import NAME_PATTERN, SelectArg
from app.services.music.types import VIDEO_ID_PATTERN


class TrackOut(BaseModel):
    video_id: str
    title: str
    artist: str | None = None
    # Solo si YouTube lo indica; nunca se inventa.
    album: str | None = None
    duration_seconds: int | None = None
    # Miniatura pública de YouTube (i.ytimg.com) y página del vídeo.
    thumbnail_url: str | None = None
    url: str | None = None
    source: Literal["youtube"] = "youtube"


class OutputOut(BaseModel):
    # Salida elegida: "aux" (jack auxiliar) o "bluetooth".
    mode: Literal["aux", "bluetooth"]
    # Salida del servidor de sonido por la que suena ahora mismo (None si no suena nada).
    sink: str | None = None


class MusicStateOut(BaseModel):
    # idle: nada cargado · loading: pedida, aún no suena · playing: suena (confirmado por FFmpeg) ·
    # paused · stopped: detenida con una pista · error: lo último falló y no suena nada.
    status: Literal["idle", "loading", "playing", "paused", "stopped", "error"]
    # Cambia con cada pista nueva o al detener.
    generation: int
    current: TrackOut | None
    upcoming: list[TrackOut]
    position_seconds: int = 0
    duration_seconds: int | None = None
    # Volumen del reproductor (0-100).
    volume: int = 0
    output: OutputOut
    # Último problema de reproducción (salida no disponible, pista que no se pudo reproducir).
    error: str | None = None
    # Modos de reproducción (los decide el servidor al acabar cada pista).
    shuffle: bool = False
    repeat_mode: Literal["off", "one", "all"] = "off"
    autoplay: bool = False
    # Sube con cada cambio visible; updated_at = momento del estado (ms desde epoch) para la posición.
    version: int = 0
    updated_at: int = 0


class MusicSearchResponse(BaseModel):
    tracks: list[TrackOut]


class ArtistOut(BaseModel):
    name: str
    # Canciones suyas entre las recomendadas.
    tracks: int


class MusicRecommendationsResponse(BaseModel):
    # Canción de partida (la actual si no se indica otra). None si no hay ninguna.
    seed: TrackOut | None
    # Mix de YouTube de la semilla: canciones relacionadas reales.
    tracks: list[TrackOut]
    # Más canciones del mismo artista (búsqueda en YouTube).
    artist_tracks: list[TrackOut]
    # Artistas que aparecen en las recomendaciones (sin el de la semilla).
    artists: list[ArtistOut]


class MusicHistoryResponse(BaseModel):
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
    # Código del fallo cuando ok es false: not_found, nothing_playing, queue_full,
    # output_unavailable, invalid_output, upstream, forbidden.
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


class OutputRequest(BaseModel):
    mode: Literal["aux", "bluetooth"]


class OutputsResponse(BaseModel):
    """Salidas (sinks) que ofrece ahora el servidor de sonido del host."""

    mode: Literal["aux", "bluetooth"]
    sinks: list[str]
