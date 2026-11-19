import re
from dataclasses import dataclass

# ID de vídeo de YouTube: 11 caracteres. Solo se reproducen IDs, nunca URLs arbitrarias (SSRF).
VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
_VIDEO_ID_RE = re.compile(VIDEO_ID_PATTERN)


def is_video_id(value: object) -> bool:
    return isinstance(value, str) and bool(_VIDEO_ID_RE.match(value))


def watch_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("video_id inválido")
    return f"https://www.youtube.com/watch?v={video_id}"


SOURCE = "youtube"


def thumbnail_url(video_id: str) -> str:
    """Miniatura pública de YouTube para un ID validado (no depende de lo que devuelva yt-dlp)."""
    if not is_video_id(video_id):
        raise ValueError("video_id inválido")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    artist: str | None = None
    duration_seconds: int | None = None
    # Solo si YouTube lo indica (vídeos de YouTube Music); nunca se inventa.
    album: str | None = None

    @property
    def url(self) -> str:
        return watch_url(self.video_id)

    @property
    def thumbnail(self) -> str:
        return thumbnail_url(self.video_id)


@dataclass(frozen=True)
class AudioSource:
    """Información que resolvió yt-dlp, guardada en un archivo privado (JSON de `extract_info`).

    yt-dlp vuelve a leerla con `--load-info-json` para descargar sin repetir la extracción. Caduca en
    horas (las URLs de YouTube): el controlador la renueva antes.
    """

    info_path: str
