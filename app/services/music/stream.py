"""Reproducción con FFmpeg: lee la URL de audio y la envía a una salida del servidor de sonido.

Un proceso FFmpeg por tramo de reproducción, sin ficheros temporales. FFmpeg escribe en PulseAudio
(`-f pulse`), que consume al ritmo real del audio, así que descarga y decodifica en tiempo real y no en
ráfaga. Pausar o cambiar de salida termina el proceso; al reanudar se abre otro desde la misma posición.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
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
# Nombre con el que aparece la música en el mezclador del host (pavucontrol, pactl list sink-inputs).
STREAM_NAME = "Lumia música"


class FfmpegPlayer:
    def __init__(self, settings: Settings, spawn: Spawn = asyncio.create_subprocess_exec) -> None:
        self._settings = settings
        self._spawn = spawn

    def build_args(self, source: AudioSource, sink: str, start_seconds: float = 0.0) -> list[str]:
        s = self._settings
        args = [
            s.music_ffmpeg_path,
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        ]
        if source.headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in source.headers.items())]
        if start_seconds > 0:
            # Antes de -i: salta en la fuente (petición HTTP con rango) sin descargar lo anterior.
            args += ["-ss", f"{start_seconds:.1f}"]
        args += ["-i", source.url, "-vn", "-ac", "2", "-ar", str(s.music_sample_rate), "-threads", "1"]
        if s.music_volume_percent != 100:
            args += ["-af", f"volume={s.music_volume_percent / 100:.2f}"]
        return [*args, "-f", "pulse", "-device", sink, STREAM_NAME]

    async def start(self, source: AudioSource, sink: str, start_seconds: float = 0.0) -> Any:
        """Lanza FFmpeg y devuelve el proceso (asyncio.subprocess.Process)."""
        try:
            return await self._spawn(
                *self.build_args(source, sink, start_seconds),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise UpstreamServiceError("FFmpeg", "FFmpeg no está instalado") from exc

    @staticmethod
    async def error_tail(proc: Any) -> str:
        """Final de la salida de error de un FFmpeg ya terminado (para el log)."""
        if proc.stderr is None:
            return ""
        return (await proc.stderr.read(STDERR_TAIL_BYTES)).decode("utf-8", "replace").strip()

    @staticmethod
    async def stop(proc: Any) -> None:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
