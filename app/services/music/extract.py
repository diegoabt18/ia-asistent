"""Único módulo que usa yt-dlp como biblioteca. Todo lo demás trabaja con `Track` y `AudioSource`.

yt-dlp es bloqueante: los métodos públicos lo ejecutan en un hilo con timeout. Solo se permite el
extractor de YouTube y solo se aceptan IDs de vídeo validados: el extractor genérico de yt-dlp
descargaría cualquier URL y abriría la puerta a peticiones a servicios internos (SSRF).

La extracción guarda la información de yt-dlp en un archivo privado (`AudioSource.info_path`): el
reproductor se la pasa a `yt-dlp --load-info-json`, que descarga el audio sin volver a extraerlo. La
URL de audio nunca se abre con FFmpeg: YouTube la rechaza (403) fuera del descargador de yt-dlp.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.types import AudioSource, Track, is_video_id, watch_url

logger = logging.getLogger(__name__)
_YTDLP_LOGGER = logging.getLogger("yt_dlp")

# Extractores permitidos (expresiones regulares sobre el nombre del extractor de yt-dlp).
ALLOWED_EXTRACTORS = ["youtube", "youtube:search"]
# Archivos de información que se conservan a la vez (la pista actual y las últimas); el resto se borra.
INFO_FILES_KEPT = 3

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


def _text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _to_track(entry: Any) -> Track | None:
    if not isinstance(entry, dict) or not is_video_id(entry.get("id")):
        return None
    title = _text(entry.get("title"), 200)
    if title is None:
        return None
    duration = entry.get("duration")
    return Track(
        video_id=entry["id"],
        title=title,
        artist=_text(entry.get("artist") or entry.get("channel") or entry.get("uploader"), 120),
        duration_seconds=int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        # Solo si YouTube lo indica (vídeos de YouTube Music).
        album=_text(entry.get("album"), 200),
    )


class YtDlpExtractor:
    def __init__(self, settings: Settings, factory: YdlFactory = _default_factory) -> None:
        self._settings = settings
        self._factory = factory
        self._dir: str | None = None
        self._files: list[str] = []

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
        """Resuelve el vídeo (mejor pista de audio) y guarda su información para descargarlo después."""
        url = watch_url(video_id)  # lanza ValueError si el ID no es válido
        options = {**_base_options(self._settings.music_extract_timeout_seconds), "format": "bestaudio/best"}
        info = await self._run(options, url, self._settings.music_extract_timeout_seconds, sanitize=True)
        track = _to_track(info)
        stream_url = info.get("url") if isinstance(info, dict) else None
        if (
            track is None
            or track.video_id != video_id
            or not isinstance(stream_url, str)
            or not stream_url.startswith("https://")
        ):
            raise UpstreamServiceError("yt-dlp", "sin audio reproducible")
        path = await asyncio.to_thread(self._save_info, video_id, info)
        return track, AudioSource(info_path=path)

    def _save_info(self, video_id: str, info: dict[str, Any]) -> str:
        """Escribe la información en un directorio privado (0700, archivo 0600) y borra las antiguas."""
        if self._dir is None or not os.path.isdir(self._dir):
            self._dir = tempfile.mkdtemp(prefix="lumia-music-")
            self._files = []
        path = os.path.join(self._dir, f"{video_id}.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info, handle)
        self._files = [f for f in self._files if f != path] + [path]
        for old in self._files[:-INFO_FILES_KEPT]:
            with contextlib.suppress(OSError):
                os.remove(old)
        self._files = self._files[-INFO_FILES_KEPT:]
        return path

    def close(self) -> None:
        """Borra los archivos de información (al apagar)."""
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir, self._files = None, []

    async def _run(self, options: dict[str, Any], target: str, timeout: float, sanitize: bool = False) -> Any:
        def work() -> Any:
            ydl = self._factory(options)
            try:
                info = ydl.extract_info(target, download=False)
                # sanitize_info deja solo datos serializables: lo que espera --load-info-json.
                return ydl.sanitize_info(info) if sanitize and hasattr(ydl, "sanitize_info") else info
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
