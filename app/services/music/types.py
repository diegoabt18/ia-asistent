import re
from dataclasses import dataclass, field

# ID de vídeo de YouTube: 11 caracteres. Solo se reproducen IDs, nunca URLs arbitrarias (SSRF).
VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
_VIDEO_ID_RE = re.compile(VIDEO_ID_PATTERN)


def is_video_id(value: object) -> bool:
    return isinstance(value, str) and bool(_VIDEO_ID_RE.match(value))


def watch_url(video_id: str) -> str:
    if not is_video_id(video_id):
        raise ValueError("video_id inválido")
    return f"https://www.youtube.com/watch?v={video_id}"


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    artist: str | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class AudioSource:
    """URL de audio directa que resolvió yt-dlp. Caduca: se resuelve justo antes de reproducir."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
