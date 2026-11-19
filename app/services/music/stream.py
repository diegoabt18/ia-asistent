"""Reproducción: yt-dlp descarga el audio y se lo pasa a FFmpeg por una tubería; FFmpeg lo decodifica y
lo envía a una salida del servidor de sonido del host (`-f pulse`).

Por qué una tubería y no FFmpeg leyendo la URL: YouTube responde 403 a cualquier petición a la URL de
audio que no haga el descargador de yt-dlp (comprobado con FFmpeg, urllib y la red de yt-dlp, con y sin
cabeceras, cookies o rangos). yt-dlp descarga con `--load-info-json` (la información ya extraída, sin
repetir la extracción) y reintenta solo si la conexión se corta.

FFmpeg informa de su progreso real (`-progress pipe:1`): con él se conoce la posición exacta y se
confirma que el audio está sonando antes de decirlo. Un par de procesos por tramo de reproducción, sin
ficheros temporales de audio. Pausar, saltar o cambiar de salida termina ambos; al reanudar se abre otro
par desde la posición (FFmpeg descarta lo anterior sin reproducirlo).
"""

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.types import AudioSource

logger = logging.getLogger(__name__)

# Firma de asyncio.create_subprocess_exec (inyectable en pruebas).
Spawn = Callable[..., Awaitable[Any]]

# FFmpeg solo lee la tubería: ningún contenido puede hacerle abrir otras URLs o archivos.
PROTOCOL_WHITELIST = "pipe"
STDERR_TAIL_BYTES = 2000
# Cómo aparece la música en el servidor de sonido (pactl list sink-inputs). En ASCII: `pactl -f json`
# no sabe representar caracteres no ASCII de estas propiedades.
STREAM_APP = "lumia-music"
STREAM_NAME = "Lumia musica"
STOP_TIMEOUT_SECONDS = 3.0

# Las URLs firmadas de YouTube no se registran (incluyen la IP del servidor y firmas).
_URL_RE = re.compile(r"https?://\S+")


def redact(text: str) -> str:
    return _URL_RE.sub("<url>", text)


class Playback:
    """Un tramo de reproducción: el descargador (yt-dlp), el decodificador (FFmpeg) y su progreso."""

    def __init__(self, downloader: Any, decoder: Any, start_seconds: float) -> None:
        self.downloader = downloader
        self.decoder = decoder
        self.start_seconds = start_seconds
        # Se activa con el primer audio que FFmpeg entrega al servidor de sonido.
        self.started = asyncio.Event()
        self._played = 0.0
        self._reader = asyncio.ensure_future(self._read_progress())

    @property
    def position(self) -> float:
        """Segundos de la pista: desde donde empezó este tramo más lo que FFmpeg ya reprodujo."""
        return self.start_seconds + self._played

    @property
    def returncode(self) -> int | None:
        return self.decoder.returncode

    async def wait(self) -> int:
        return await self.decoder.wait()

    async def downloader_code(self, timeout: float = 3.0) -> int | None:
        """Código de salida de yt-dlp (None si no terminó en ese tiempo)."""
        with contextlib.suppress(asyncio.TimeoutError):
            return await asyncio.wait_for(self.downloader.wait(), timeout)
        return None

    async def _read_progress(self) -> None:
        stdout = getattr(self.decoder, "stdout", None)
        if stdout is None:
            return
        while True:
            line = await stdout.readline()
            if not line:
                return
            key, _, value = line.decode("ascii", "replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit() and int(value) > 0:
                self._played = int(value) / 1_000_000
                self.started.set()

    def close_reader(self) -> None:
        self._reader.cancel()


class FfmpegPlayer:
    def __init__(self, settings: Settings, spawn: Spawn = asyncio.create_subprocess_exec) -> None:
        self._settings = settings
        self._spawn = spawn

    def downloader_args(self, source: AudioSource) -> list[str]:
        s = self._settings
        return [
            s.music_ytdlp_path,
            "--ignore-config", "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
            "--load-info-json", source.info_path,
            "--format", "bestaudio/best",
            # Si la conexión se corta, yt-dlp sigue donde iba (petición con rango).
            "--retries", "10", "--fragment-retries", "10",
            "--no-part", "--output", "-",
        ]

    def decoder_args(self, sink: str, start_seconds: float = 0.0) -> list[str]:
        s = self._settings
        args = [
            s.music_ffmpeg_path,
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-i", "pipe:0",
        ]
        if start_seconds > 0:
            # Tras -i: la tubería no permite saltar; FFmpeg decodifica y descarta hasta la posición.
            args += ["-ss", f"{start_seconds:.1f}"]
        args += ["-vn", "-ac", "2", "-ar", str(s.music_sample_rate), "-threads", "1"]
        if s.music_volume_percent != 100:
            args += ["-af", f"volume={s.music_volume_percent / 100:.2f}"]
        return [
            *args,
            # Progreso cada 0,2 s: confirma pronto que suena y da la posición exacta.
            "-progress", "pipe:1", "-nostats", "-stats_period", "0.2",
            "-f", "pulse", "-name", STREAM_APP, "-device", sink, STREAM_NAME,
        ]

    async def start(self, source: AudioSource, sink: str, start_seconds: float = 0.0) -> Playback:
        """Lanza yt-dlp y FFmpeg unidos por una tubería y devuelve el tramo de reproducción."""
        read_fd, write_fd = os.pipe()
        try:
            try:
                downloader = await self._spawn(
                    *self.downloader_args(source),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=write_fd,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise UpstreamServiceError("yt-dlp", "yt-dlp no está instalado en el servidor") from exc
            try:
                decoder = await self._spawn(
                    *self.decoder_args(sink, start_seconds),
                    stdin=read_fd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                await self._terminate(downloader)
                raise UpstreamServiceError("FFmpeg", "FFmpeg no está instalado en el servidor") from exc
        finally:
            # Cada proceso ya tiene su extremo; los nuestros se cierran para que el fin de datos llegue.
            os.close(read_fd)
            os.close(write_fd)
        return Playback(downloader, decoder, start_seconds)

    @staticmethod
    async def _tail(proc: Any) -> str:
        stderr = getattr(proc, "stderr", None)
        if stderr is None:
            return ""
        with contextlib.suppress(Exception):
            data = await asyncio.wait_for(stderr.read(STDERR_TAIL_BYTES), 1.0)
            return redact(data.decode("utf-8", "replace").strip())
        return ""

    async def error_tail(self, playback: Playback) -> str:
        """Últimos mensajes de error de FFmpeg y de yt-dlp, sin URLs."""
        decoder = await self._tail(playback.decoder)
        downloader = await self._tail(playback.downloader)
        parts = [f"ffmpeg: {decoder}" if decoder else "", f"yt-dlp: {downloader}" if downloader else ""]
        return " | ".join(p for p in parts if p)

    @staticmethod
    async def _terminate(proc: Any) -> None:
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT_SECONDS)

    async def stop(self, playback: Playback) -> None:
        """Termina el tramo: primero FFmpeg (deja de sonar al instante) y después yt-dlp."""
        await self._terminate(playback.decoder)
        await self._terminate(playback.downloader)
        playback.close_reader()
