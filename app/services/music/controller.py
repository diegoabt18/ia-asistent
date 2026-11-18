"""Coordinación del módulo de música: única puerta de entrada para la API, los comandos y el ESP32.

Una instancia por proceso (ver main.lifespan). Las operaciones que cambian la cola o el estado se
serializan con un lock; la búsqueda y la extracción (lentas) ocurren fuera de él.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.extract import YtDlpExtractor
from app.services.music.player import DeviceReport, DeviceTracker, PlayerState, PlayerStatus
from app.services.music.queue import MusicQueue
from app.services.music.search import MusicSearchService
from app.services.music.stream import FfmpegTranscoder
from app.services.music.types import AudioSource, Track

logger = logging.getLogger(__name__)

# Las URLs de audio de YouTube caducan en horas; se reutilizan unos minutos para que las
# reconexiones del ESP32 no repitan la extracción.
SOURCE_CACHE_SECONDS = 600.0


class NothingToPlayError(Exception):
    pass


@dataclass(frozen=True)
class MusicSnapshot:
    status: PlayerStatus
    generation: int
    current: Track | None
    upcoming: list[Track]
    device_connected: bool
    device: DeviceReport | None
    device_last_seen_seconds: float | None
    device_reconnects: int
    streaming: bool


@dataclass(frozen=True)
class StreamSession:
    track: Track
    generation: int
    media_type: str
    headers: dict[str, str]
    chunks: AsyncIterator[bytes]


class MusicController:
    def __init__(
        self,
        settings: Settings,
        extractor: YtDlpExtractor | None = None,
        transcoder: FfmpegTranscoder | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._extractor = extractor or YtDlpExtractor(settings)
        self._search = MusicSearchService(settings, self._extractor)
        self._transcoder = transcoder or FfmpegTranscoder(settings)
        self._clock = clock
        self._queue = MusicQueue(settings.music_queue_max_items)
        self._player = PlayerState()
        self._device = DeviceTracker(settings.music_device_timeout_seconds, clock)
        self._lock = asyncio.Lock()
        self._stream_token = 0
        self._streams_open = 0
        self._source_cache: tuple[str, AudioSource, float] | None = None

    # --- Búsqueda y resolución -------------------------------------------------------------

    async def search(self, query: str, limit: int | None = None) -> list[Track]:
        return await self._search.search(query, limit)

    async def _find(self, query: str | None, video_id: str | None) -> Track:
        """Pista a partir de un ID (se extrae para validar y obtener el título) o de una búsqueda."""
        if video_id:
            track, source = await self._extractor.extract(video_id)
            if not self._search.is_playable(track):
                raise NothingToPlayError("la pista es demasiado larga")
            self._source_cache = (track.video_id, source, self._clock())
            return track
        results = await self._search.search(query or "", 1)
        if not results:
            raise NothingToPlayError("sin resultados")
        return results[0]

    async def _source_for(self, track: Track) -> AudioSource:
        cached = self._source_cache
        if cached and cached[0] == track.video_id and self._clock() - cached[2] < SOURCE_CACHE_SECONDS:
            return cached[1]
        _, source = await self._extractor.extract(track.video_id)
        self._source_cache = (track.video_id, source, self._clock())
        return source

    # --- Control ---------------------------------------------------------------------------

    async def play(self, query: str | None = None, video_id: str | None = None) -> Track:
        """Con búsqueda o ID: reproduce esa pista ya. Sin nada: reanuda o empieza la cola."""
        if query or video_id:
            track = await self._find(query, video_id)
            async with self._lock:
                self._queue.play_now(track)
                self._player.start_track()
            return track
        async with self._lock:
            current = self._queue.current
            if self._player.status == "paused" and current:
                self._player.resume()
                return current
            if self._player.status == "playing" and current:
                return current
            track = self._queue.start()
            if track is None:
                raise NothingToPlayError("la cola está vacía")
            self._player.start_track()
            return track

    async def enqueue(self, query: str | None = None, video_id: str | None = None) -> Track:
        track = await self._find(query, video_id)
        async with self._lock:
            self._queue.add(track)
        return track

    async def pause(self) -> bool:
        async with self._lock:
            return self._player.pause()

    async def resume(self) -> bool:
        async with self._lock:
            return self._player.resume()

    async def next(self) -> Track | None:
        """Pasa a la siguiente. Si no hay, no cambia nada (la canción actual sigue sonando)."""
        async with self._lock:
            if not self._queue.upcoming:
                return None
            return self._advance()

    def _advance(self) -> Track | None:
        track = self._queue.next()
        if track:
            self._player.start_track()
        else:
            self._player.stop()
        return track

    async def previous(self) -> Track | None:
        async with self._lock:
            track = self._queue.previous()
            if track:
                self._player.start_track()
            return track

    async def stop(self) -> None:
        async with self._lock:
            self._player.stop()

    async def clear(self) -> None:
        async with self._lock:
            self._queue.clear()
            self._player.stop()

    # --- Estado y dispositivo --------------------------------------------------------------

    def snapshot(self) -> MusicSnapshot:
        return MusicSnapshot(
            status=self._player.status,
            generation=self._player.generation,
            current=self._queue.current,
            upcoming=self._queue.upcoming,
            device_connected=self._device.connected,
            device=self._device.report,
            device_last_seen_seconds=self._device.seconds_since_seen(),
            device_reconnects=self._device.reconnects,
            streaming=self._streams_open > 0,
        )

    def heartbeat(self, report: DeviceReport) -> MusicSnapshot:
        if self._device.heartbeat(report):
            logger.info("ESP32 %s conectado", report.device_id)
        return self.snapshot()

    # --- Stream ----------------------------------------------------------------------------

    async def open_stream(self) -> StreamSession | None:
        """Prepara el audio de la pista actual. None si no hay nada que reproducir.

        Solo hay un stream activo: abrir otro cierra el anterior (p. ej. el ESP32 reconecta).
        """
        track = self._queue.current
        if track is None or self._player.status == "idle":
            return None
        generation = self._player.generation
        source = await self._source_for(track)  # UpstreamServiceError -> 502 antes de empezar
        self._stream_token += 1
        self._player.notify()  # despierta a un stream anterior en pausa para que se cierre
        return StreamSession(
            track=track,
            generation=generation,
            media_type=self._transcoder.media_type,
            headers=self._transcoder.audio_headers(),
            chunks=self._pump(source, generation, self._stream_token),
        )

    def _superseded(self, generation: int, token: int) -> bool:
        return generation != self._player.generation or token != self._stream_token

    async def _pump(self, source: AudioSource, generation: int, token: int) -> AsyncIterator[bytes]:
        self._streams_open += 1
        finished = False
        try:
            async with aclosing(self._transcoder.stream(source)) as chunks:
                async for chunk in chunks:
                    while self._player.status == "paused" and not self._superseded(generation, token):
                        await self._player.wait_change()
                    if self._superseded(generation, token):
                        return
                    yield chunk
            finished = True
        except UpstreamServiceError as exc:
            # A mitad del stream ya no se puede cambiar el código HTTP: se registra y se corta.
            logger.warning("Stream interrumpido: %s", exc)
        finally:
            self._streams_open -= 1
        if finished and not self._superseded(generation, token):
            # Pista terminada: pasa a la siguiente. El ESP32 verá otra generación y reconectará.
            async with self._lock:
                if not self._superseded(generation, token):
                    self._advance()

    async def close(self) -> None:
        """Al apagar: invalida el stream activo para que su FFmpeg se cierre."""
        self._stream_token += 1
        self._player.stop()
