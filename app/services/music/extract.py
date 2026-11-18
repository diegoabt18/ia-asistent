"""Único módulo que usa yt-dlp. Todo lo demás trabaja con `Track` y `AudioSource`.

yt-dlp es bloqueante: los métodos públicos lo ejecutan en un hilo con timeout. Solo se permite el
extractor de YouTube y solo se aceptan IDs de vídeo validados: el extractor genérico de yt-dlp
descargaría cualquier URL y abriría la puerta a peticiones a servicios internos (SSRF).
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.types import AudioSource, Track, is_video_id, watch_url

logger = logging.getLogger(__name__)
_YTDLP_LOGGER = logging.getLogger("yt_dlp")

# Extractores permitidos (expresiones regulares sobre el nombre del extractor de yt-dlp).
ALLOWED_EXTRACTORS = ["youtube", "youtube:search"]
# Cabeceras de la respuesta de yt-dlp que FFmpeg necesita para descargar el audio.
FORWARDED_HEADERS = ("User-Agent", "Accept", "Accept-Language", "Referer", "Origin", "Cookie")

# Recibe las opciones de yt-dlp y devuelve un objeto con extract_info(url, download=False).
YdlFactory = Callable[[dict[str, Any]], Any]


def _default_factory(options: dict[str, Any]) -> Any:
    # Importación tardía: el resto de la API (y sus pruebas) no necesita yt-dlp instalado.
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:  # pragma: no cover - depende de la instalación
        raise UpstreamServiceError("yt-dlp", "yt-dlp no está instalado") from exc
    return YoutubeDL(options)


def _base_options(timeout: float) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        # Mensajes de yt-dlp al logging del proceso, no directamente a stderr.
        "logger": _YTDLP_LOGGER,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout,
        # Coincidencia exacta (fullmatch): "youtube" no incluye youtube:tab ni youtube:playlist.
        "allowed_extractors": ALLOWED_EXTRACTORS,
    }


def _to_track(entry: Any) -> Track | None:
    if not isinstance(entry, dict) or not is_video_id(entry.get("id")):
        return None
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    artist = entry.get("artist") or entry.get("channel") or entry.get("uploader")
    duration = entry.get("duration")
    return Track(
        video_id=entry["id"],
        title=title.strip()[:200],
        artist=artist.strip()[:120] if isinstance(artist, str) and artist.strip() else None,
        duration_seconds=int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
    )


class YtDlpExtractor:
    def __init__(self, settings: Settings, factory: YdlFactory = _default_factory) -> None:
        self._settings = settings
        self._factory = factory

    async def search(self, query: str, limit: int) -> list[Track]:
        """Busca en YouTube (`ytsearchN:`). Lista plana: no resuelve el audio de cada resultado."""
        options = {**_base_options(self._settings.music_search_timeout_seconds), "extract_flat": "in_playlist"}
        info = await self._run(
            options, f"ytsearch{limit}:{query}", self._settings.music_search_timeout_seconds
        )
        entries = info.get("entries") if isinstance(info, dict) else None
        tracks = [t for t in (_to_track(e) for e in entries or []) if t is not None]
        return tracks[:limit]

    async def extract(self, video_id: str) -> tuple[Track, AudioSource]:
        """Resuelve la URL de audio directa del vídeo (la mejor pista solo de audio)."""
        url = watch_url(video_id)  # lanza ValueError si el ID no es válido
        options = {**_base_options(self._settings.music_extract_timeout_seconds), "format": "bestaudio/best"}
        info = await self._run(options, url, self._settings.music_extract_timeout_seconds)
        track = _to_track(info)
        stream_url = info.get("url") if isinstance(info, dict) else None
        if track is None or not isinstance(stream_url, str) or not stream_url.startswith("https://"):
            raise UpstreamServiceError("yt-dlp", "sin audio reproducible")
        raw_headers = info.get("http_headers") or {}
        headers = {
            k: str(v) for k, v in raw_headers.items() if k in FORWARDED_HEADERS and "\n" not in str(v)
        }
        return track, AudioSource(url=stream_url, headers=headers)

    async def _run(self, options: dict[str, Any], target: str, timeout: float) -> Any:
        def work() -> Any:
            ydl = self._factory(options)
            try:
                return ydl.extract_info(target, download=False)
            finally:
                close = getattr(ydl, "close", None)
                if callable(close):
                    close()

        try:
            return await asyncio.wait_for(asyncio.to_thread(work), timeout=timeout + 5)
        except UpstreamServiceError:
            raise
        except asyncio.TimeoutError as exc:
            raise UpstreamServiceError("yt-dlp", "tiempo de espera agotado") from exc
        except Exception as exc:  # yt-dlp lanza DownloadError/ExtractorError y otros
            logger.warning("yt-dlp falló con %s: %s", target[:80], exc)
            raise UpstreamServiceError("yt-dlp", exc.__class__.__name__) from exc
