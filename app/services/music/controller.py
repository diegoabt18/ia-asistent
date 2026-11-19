"""Coordinación del módulo de música: única puerta de entrada para la API y los comandos.

Una instancia por proceso (ver main.lifespan). Las operaciones que cambian la cola o el estado se
serializan con un lock; la búsqueda y la extracción (lentas) ocurren fuera de él.

La música suena en el propio servidor. Un bucle en segundo plano (`_run`) mira el estado y mantiene un
FFmpeg reproduciendo la pista actual en la salida elegida: si el estado cambia (pausa, otra pista,
otra salida) termina ese FFmpeg y, si toca, abre otro. Al acabar una pista pasa a la siguiente.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.extract import YtDlpExtractor
from app.services.music.output import AudioOutputs, OutputMode, OutputUnavailableError
from app.services.music.player import PlayerState, PlayerStatus
from app.services.music.queue import MusicQueue
from app.services.music.search import MusicSearchService
from app.services.music.stream import FfmpegPlayer
from app.services.music.types import AudioSource, Track

logger = logging.getLogger(__name__)

# Las URLs de audio de YouTube caducan en horas; se reutilizan unos minutos para que reanudar o
# cambiar de salida no repita la extracción.
SOURCE_CACHE_SECONDS = 600.0


class NothingToPlayError(Exception):
    pass


@dataclass(frozen=True)
class MusicSnapshot:
    status: PlayerStatus
    generation: int
    current: Track | None
    upcoming: list[Track]
    output_mode: OutputMode
    # Salida por la que suena ahora mismo (None si no suena nada).
    sink: str | None
    position_seconds: int
    # Último problema de reproducción (salida no disponible, pista que no se pudo reproducir).
    error: str | None


class MusicController:
    def __init__(
        self,
        settings: Settings,
        extractor: YtDlpExtractor | None = None,
        player: FfmpegPlayer | None = None,
        outputs: AudioOutputs | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._extractor = extractor or YtDlpExtractor(settings)
        self._search = MusicSearchService(settings, self._extractor)
        self._ffmpeg = player or FfmpegPlayer(settings)
        self._outputs = outputs or AudioOutputs(settings)
        self._clock = clock
        self._queue = MusicQueue(settings.music_queue_max_items)
        self._player = PlayerState()
        self._lock = asyncio.Lock()
        self._source_cache: tuple[str, AudioSource, float] | None = None
        self._output_mode: OutputMode = settings.music_output_default
        self._sink: str | None = None
        self._playing_since: float | None = None
        # Segundos ya reproducidos de la pista `_position_generation` (para reanudar donde iba).
        self._position = 0.0
        self._position_generation = -1
        self._error: str | None = None
        self._runner: asyncio.Task[None] | None = None
        self._closed = False

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
        """Con búsqueda o ID: reproduce esa pista ya. Sin nada: reanuda o empieza la cola.

        Comprueba antes la salida: si no hay por dónde sonar, lanza OutputUnavailableError sin tocar la cola.
        """
        await self._outputs.resolve(self._output_mode)
        self._ensure_runner()
        if query or video_id:
            track = await self._find(query, video_id)
            async with self._lock:
                self._queue.play_now(track)
                self._start_track()
            return track
        async with self._lock:
            current = self._queue.current
            if self._player.status == "paused" and current:
                self._error = None
                self._player.resume()
                return current
            if self._player.status == "playing" and current:
                return current
            track = self._queue.start()
            if track is None:
                raise NothingToPlayError("la cola está vacía")
            self._start_track()
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
        if self._player.status != "paused":
            return False
        await self._outputs.resolve(self._output_mode)
        self._ensure_runner()
        async with self._lock:
            self._error = None
            return self._player.resume()

    async def next(self) -> Track | None:
        """Pasa a la siguiente. Si no hay, no cambia nada (la canción actual sigue sonando)."""
        async with self._lock:
            if not self._queue.upcoming:
                return None
            return self._advance()

    def _start_track(self) -> None:
        self._error = None
        self._player.start_track()

    def _advance(self) -> Track | None:
        track = self._queue.next()
        if track:
            self._start_track()
        else:
            self._player.stop()
        return track

    async def previous(self) -> Track | None:
        async with self._lock:
            track = self._queue.previous()
            if track:
                self._start_track()
            return track

    async def stop(self) -> None:
        async with self._lock:
            self._player.stop()

    async def clear(self) -> None:
        async with self._lock:
            self._queue.clear()
            self._player.stop()

    async def set_output(self, mode: OutputMode) -> str:
        """Cambia la salida (aux o Bluetooth). Si algo suena, sigue en la nueva salida desde donde iba.

        Devuelve el sink elegido o lanza OutputUnavailableError (sin cambiar la salida actual).
        """
        sink = await self._outputs.resolve(mode)
        async with self._lock:
            self._output_mode = mode
            self._error = None
            self._player.notify()
        return sink

    async def outputs(self) -> list[str]:
        """Salidas que ofrece ahora el servidor de sonido (OutputUnavailableError si no responde)."""
        return await self._outputs.list_sinks()

    async def resolve_output(self, mode: OutputMode) -> str:
        """Sink que usaría ese modo ahora mismo, sin cambiar nada (OutputUnavailableError si no hay)."""
        return await self._outputs.resolve(mode)

    # --- Estado ----------------------------------------------------------------------------

    def snapshot(self) -> MusicSnapshot:
        position = self._position if self._position_generation == self._player.generation else 0.0
        if self._playing_since is not None:
            position += self._clock() - self._playing_since
        return MusicSnapshot(
            status=self._player.status,
            generation=self._player.generation,
            current=self._queue.current,
            upcoming=self._queue.upcoming,
            output_mode=self._output_mode,
            sink=self._sink,
            position_seconds=int(position),
            error=self._error,
        )

    # --- Reproducción en segundo plano -----------------------------------------------------

    def _ensure_runner(self) -> None:
        if not self._closed and (self._runner is None or self._runner.done()):
            self._runner = asyncio.create_task(self._run(), name="music-player")

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - el bucle no debe morir por un fallo inesperado
                logger.exception("Error en el reproductor de música")
                async with self._lock:
                    self._error = "error interno del reproductor"
                    self._player.pause()

    async def _step(self) -> None:
        """Una vuelta: espera a que haya algo que reproducir, lo reproduce y reacciona a cómo terminó."""
        changed = self._player.change_event()
        track = self._queue.current
        if self._player.status != "playing" or track is None:
            await changed.wait()
            return
        generation = self._player.generation
        if generation != self._position_generation:
            self._position_generation = generation
            self._position = 0.0
        try:
            sink = await self._outputs.resolve(self._output_mode)
            source = await self._source_for(track)
        except OutputUnavailableError as exc:
            await self._fail_output(exc, generation)
            return
        except UpstreamServiceError as exc:
            await self._fail_track(track, generation, str(exc))
            return
        if changed.is_set():
            return  # el estado cambió mientras se preparaba: se vuelve a evaluar
        try:
            proc = await self._ffmpeg.start(source, sink, self._position)
        except UpstreamServiceError as exc:
            await self._fail_output(OutputUnavailableError(self._output_mode, exc.detail), generation)
            return

        self._sink = sink
        self._playing_since = self._clock()
        ended = asyncio.ensure_future(proc.wait())
        waiter = asyncio.ensure_future(changed.wait())
        try:
            await asyncio.wait({ended, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await self._ffmpeg.stop(proc)
            await asyncio.gather(ended, return_exceptions=True)
            elapsed = self._clock() - self._playing_since
            self._playing_since = None
            self._sink = None
            if generation == self._position_generation:
                self._position += elapsed

        if changed.is_set():
            return  # pausa, otra pista u otra salida: lo decide la siguiente vuelta
        if proc.returncode == 0:
            async with self._lock:
                if generation == self._player.generation and self._player.status == "playing":
                    self._advance()  # pista terminada
            return
        tail = await self._ffmpeg.error_tail(proc)
        logger.warning("FFmpeg terminó con código %s: %s", proc.returncode, tail[-500:])
        try:
            await self._outputs.resolve(self._output_mode)
        except OutputUnavailableError as exc:
            await self._fail_output(exc, generation)  # la salida desapareció (p. ej. parlante apagado)
            return
        self._source_cache = None  # la URL pudo caducar: la próxima vez se extrae de nuevo
        await self._fail_track(track, generation, f"FFmpeg código {proc.returncode}")

    async def _fail_output(self, exc: OutputUnavailableError, generation: int) -> None:
        """Sin salida: se pausa (no se pierde la cola) hasta que el usuario reanude o cambie de salida."""
        logger.warning("Salida de audio %s no disponible: %s", exc.mode, exc.reason)
        async with self._lock:
            if generation == self._player.generation:
                self._error = f"sin salida de audio: {exc.reason}"
                self._player.pause()

    async def _fail_track(self, track: Track, generation: int, reason: str) -> None:
        """La pista no se pudo reproducir: se salta a la siguiente."""
        logger.warning("No se pudo reproducir %s: %s", track.video_id, reason)
        async with self._lock:
            if generation == self._player.generation:
                self._advance()
                self._error = f"no se pudo reproducir «{track.title}»"

    async def close(self) -> None:
        """Al apagar: detiene el bucle y su FFmpeg."""
        self._closed = True
        self._player.stop()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
