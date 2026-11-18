"""Transcodificación con FFmpeg: lee la URL de audio y entrega MP3 o PCM por trozos.

Un proceso FFmpeg por stream abierto, sin ficheros temporales. La memoria queda acotada por el tamaño
de trozo y el buffer de la tubería: FFmpeg solo avanza cuando el ESP32 lee (contrapresión de TCP), así
que transcodifica al ritmo de reproducción y no en ráfaga.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.types import AudioSource

logger = logging.getLogger(__name__)

# Firma de asyncio.create_subprocess_exec (inyectable en pruebas).
Spawn = Callable[..., Awaitable[Any]]

# FFmpeg solo puede abrir HTTPS (la URL viene de yt-dlp; nada de file:, concat:, etc.).
PROTOCOL_WHITELIST = "https,tls,tcp"
STDERR_TAIL_BYTES = 2000


class FfmpegTranscoder:
    def __init__(self, settings: Settings, spawn: Spawn = asyncio.create_subprocess_exec) -> None:
        self._settings = settings
        self._spawn = spawn

    @property
    def media_type(self) -> str:
        return "audio/mpeg" if self._settings.music_output_format == "mp3" else "application/octet-stream"

    def audio_headers(self) -> dict[str, str]:
        """Cabeceras que describen el formato al ESP32 (necesarias con PCM, informativas con MP3)."""
        return {
            "X-Audio-Format": "mp3" if self._settings.music_output_format == "mp3" else "pcm_s16le",
            "X-Audio-Sample-Rate": str(self._settings.music_sample_rate),
            "X-Audio-Channels": "2",
        }

    def build_args(self, source: AudioSource) -> list[str]:
        s = self._settings
        args = [
            s.music_ffmpeg_path,
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        ]
        if source.headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in source.headers.items())]
        args += ["-i", source.url, "-vn", "-ac", "2", "-ar", str(s.music_sample_rate), "-threads", "1"]
        if s.music_output_format == "mp3":
            args += ["-c:a", "libmp3lame", "-b:a", f"{s.music_bitrate_kbps}k", "-f", "mp3"]
        else:
            args += ["-c:a", "pcm_s16le", "-f", "s16le"]
        return [*args, "pipe:1"]

    async def stream(self, source: AudioSource) -> AsyncIterator[bytes]:
        """Entrega el audio por trozos. Si el consumidor deja de leer o se cancela, mata FFmpeg."""
        try:
            proc = await self._spawn(
                *self.build_args(source),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise UpstreamServiceError("FFmpeg", "FFmpeg no está instalado") from exc
        try:
            while True:
                chunk = await proc.stdout.read(self._settings.music_chunk_bytes)
                if not chunk:
                    break
                yield chunk
            code = await proc.wait()
            if code != 0:
                tail = (await proc.stderr.read(STDERR_TAIL_BYTES)).decode("utf-8", "replace").strip()
                logger.warning("FFmpeg terminó con código %s: %s", code, tail)
                raise UpstreamServiceError("FFmpeg", f"código de salida {code}")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
