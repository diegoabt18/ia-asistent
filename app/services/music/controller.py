"""Coordinación del módulo de música: única puerta de entrada para la API y los comandos.

Una instancia por proceso (ver main.lifespan). Las operaciones que cambian la cola o el estado se
serializan con un lock; la búsqueda y la extracción (lentas) ocurren fuera de él.

La música suena en el propio servidor. Un bucle en segundo plano (`_run`) mira el estado y mantiene un
tramo de reproducción (yt-dlp → FFmpeg → salida de audio, ver stream.py) con la pista actual: si el
estado cambia (pausa, otra pista, otra salida, otra posición) termina ese tramo y, si toca, abre otro.
Al acabar una pista pasa a la siguiente.

Nada se da por «sonando» hasta que FFmpeg informa de audio reproducido (`Playback.started`): mientras
tanto el estado es `loading`. Los comandos esperan esa confirmación (`wait_until_playing`).
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.extract import YtDlpExtractor
from app.services.music.output import AudioOutputs, OutputMode, OutputUnavailableError
from app.services.music.player import PlayerState
from app.services.music.queue import MusicQueue
from app.services.music.search import MusicSearchService
from app.services.music.stream import FfmpegPlayer, Playback
from app.services.music.types import AudioSource, Track

logger = logging.getLogger(__name__)

# La información de yt-dlp (URLs de YouTube) caduca en horas; se reutiliza unos minutos para que
# reanudar, saltar o cambiar de salida no repita la extracción.
SOURCE_CACHE_SECONDS = 600.0
FAILURES_KEPT = 20

# Estado visible del reproductor:
#   idle     nada cargado            loading  se pidió reproducir y aún no suena
#   playing  suena (confirmado)      paused   en pausa
#   stopped  detenido con una pista  error    lo último falló y no suena nada
SnapshotStatus = Literal["idle", "loading", "playing", "paused", "stopped", "error"]
WaitOutcome = Literal["playing", "loading", "failed", "superseded"]


class NothingToPlayError(Exception):
    pass


@dataclass(frozen=True)
class MusicSnapshot:
    status: SnapshotStatus
    generation: int
    current: Track | None
    upcoming: list[Track]
    output_mode: OutputMode
    # Salida por la que suena ahora mismo (None si no suena nada).
    sink: str | None
    position_seconds: int
    # Volumen del reproductor (0-100).
    volume: int
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
        self._volume = settings.music_volume_default
        self._sink: str | None = None
        # Tramo en curso y su generación.
        self._playback: Playback | None = None
        self._playback_generation = -1
        # Cada tramo nuevo suma 1; `_started_segment` es el último cuyo audio se confirmó.
        self._segment = 0
        self._started_segment = 0
        self._started_generation = -1
        # Posición (segundos) de la pista `_position_generation`, para reanudar donde iba.
        self._position = 0.0
        self._position_generation = -1
        self._seek_to: float | None = None
        self._retries = 0
        self._failures: dict[int, str] = {}
        self._activity = asyncio.Event()
        self._error: str | None = None
        self.last_started_generation = 0
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

    @property
    def segment(self) -> int:
        """Contador de tramos. Se lee ANTES de un comando para esperar al tramo que abra."""
        return self._segment

    async def play(self, query: str | None = None, video_id: str | None = None) -> Track:
        """Con búsqueda o ID: reproduce esa pista ya. Sin nada: reanuda, o vuelve a empezar la pista
        detenida, o empieza la cola.

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

    async def previous(self) -> Track | None:
        async with self._lock:
            track = self._queue.previous()
            if track:
                self._start_track()
            return track

    async def jump(self, position: int) -> Track | None:
        """Reproduce la pista `position` de las pendientes (1 = la siguiente). None si no existe."""
        self._ensure_runner()
        async with self._lock:
            track = self._queue.skip(position)
            if track:
                self._start_track()
            return track

    async def stop(self) -> bool:
        """Detiene la reproducción y conserva la pista (reproducir la empieza de nuevo)."""
        async with self._lock:
            if self._player.status == "idle":
                return False
            self._error = None
            self._player.stop()
            return True

    async def clear(self) -> None:
        async with self._lock:
            self._queue.clear()
            self._player.stop()

    async def seek(self, seconds: float) -> float | None:
        """Va a esa posición de la pista actual (sonando o en pausa). None si no hay pista."""
        async with self._lock:
            current = self._queue.current
            if current is None or self._player.status not in ("playing", "paused"):
                return None
            limit = (current.duration_seconds - 1) if current.duration_seconds else None
            target = max(0.0, float(seconds))
            if limit is not None:
                target = min(target, float(max(0, limit)))
            self._position_generation = self._player.generation
            if self._playback is not None:
                self._seek_to = target  # lo aplica el bucle al cerrar el tramo actual
            else:
                self._position = target
            self._player.notify()
            return target

    async def set_volume(self, level: int) -> int:
        """Cambia el volumen (0-100) al momento, sin cortar el audio. Se aplica también a los tramos nuevos."""
        self._volume = max(0, min(100, int(level)))
        if self._playback is not None and self._started_segment == self._segment:
            await self._apply_volume()
        return self._volume

    async def _apply_volume(self) -> None:
        """Lleva el volumen del audio al del reproductor; si cambia mientras tanto, lo vuelve a aplicar."""
        applied: int | None = None
        while applied != self._volume:
            applied = self._volume
            if not await self._outputs.set_stream_volume(applied):
                logger.info("Volumen no aplicado: pactl no encontró el audio de la música")
                return

    @property
    def volume(self) -> int:
        return self._volume

    def _start_track(self) -> None:
        self._error = None
        self._retries = 0
        self._player.start_track()
        self.last_started_generation = self._player.generation

    def _advance(self) -> Track | None:
        track = self._queue.next()
        if track:
            self._start_track()
        else:
            self._player.stop()
        return track

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

    # --- Confirmación ----------------------------------------------------------------------

    def _bump(self) -> None:
        """Despierta a quien espera una confirmación o un fallo."""
        activity, self._activity = self._activity, asyncio.Event()
        activity.set()

    def failure(self, generation: int) -> str | None:
        return self._failures.get(generation)

    async def wait_until_playing(self, generation: int, after_segment: int, timeout: float) -> WaitOutcome:
        """Espera a que la pista `generation` suene en un tramo posterior a `after_segment`.

        `playing`: FFmpeg confirmó audio. `failed`: no se pudo reproducir (ver `failure`).
        `superseded`: otra orden cambió la pista o paró. `loading`: aún no suena al agotar el tiempo.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            if generation in self._failures:
                return "failed"
            if self._started_generation == generation and self._started_segment > after_segment:
                return "playing"
            if self._player.generation != generation or self._player.status != "playing":
                return "superseded"
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "loading"
            activity = self._activity
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(activity.wait(), remaining)

    # --- Estado ----------------------------------------------------------------------------

    def _status(self) -> SnapshotStatus:
        status = self._player.status
        if status == "playing":
            playback = self._playback
            # Un salto pendiente reinicia el tramo: hasta que el nuevo suene, el estado es «cargando».
            confirmed = (
                playback is not None
                and playback.started.is_set()
                and self._playback_generation == self._player.generation
                and self._seek_to is None
            )
            return "playing" if confirmed else "loading"
        if status == "paused":
            return "paused"
        if self._error:
            return "error"
        return "stopped" if self._queue.current is not None else "idle"

    def snapshot(self) -> MusicSnapshot:
        generation = self._player.generation
        playback = self._playback
        if playback is not None and self._playback_generation == generation and self._seek_to is None:
            position = playback.position
        elif self._position_generation == generation:
            position = self._seek_to if self._seek_to is not None else self._position
        else:
            position = 0.0
        return MusicSnapshot(
            status=self._status(),
            generation=generation,
            current=self._queue.current,
            upcoming=self._queue.upcoming,
            output_mode=self._output_mode,
            sink=self._sink,
            position_seconds=int(position),
            volume=self._volume,
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
                self._bump()

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
            self._seek_to = None
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
            playback = await self._ffmpeg.start(source, sink, self._position)
        except UpstreamServiceError as exc:
            await self._fail_track(track, generation, exc.detail)
            return

        self._segment += 1
        segment = self._segment
        self._playback = playback
        self._playback_generation = generation
        self._sink = sink
        ended = asyncio.ensure_future(playback.wait())
        waiter = asyncio.ensure_future(changed.wait())
        started = asyncio.ensure_future(playback.started.wait())
        try:
            done, _ = await asyncio.wait({ended, waiter, started}, return_when=asyncio.FIRST_COMPLETED)
            if started in done and not ended.done() and not waiter.done():
                # Volumen antes de anunciar «sonando»: así nadie puede cambiarlo en medio.
                await self._apply_volume()
                self._started_segment = segment
                self._started_generation = generation
                self._retries = 0
                self._bump()
                await asyncio.wait({ended, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            started.cancel()
            await self._ffmpeg.stop(playback)
            await asyncio.gather(ended, return_exceptions=True)
            self._playback = None
            self._sink = None
            if generation == self._position_generation:
                if self._seek_to is not None:
                    self._position, self._seek_to = self._seek_to, None
                else:
                    self._position = playback.position

        if changed.is_set():
            return  # pausa, otra pista, otra salida u otra posición: lo decide la siguiente vuelta
        downloader = await playback.downloader_code()
        if playback.returncode == 0 and downloader in (0, None):
            async with self._lock:
                if generation == self._player.generation and self._player.status == "playing":
                    self._advance()  # pista terminada
            return
        tail = await self._ffmpeg.error_tail(playback)
        logger.warning(
            "Reproducción interrumpida (FFmpeg %s, yt-dlp %s): %s", playback.returncode, downloader, tail[-500:]
        )
        try:
            await self._outputs.resolve(self._output_mode)
        except OutputUnavailableError as exc:
            await self._fail_output(exc, generation)  # la salida desapareció (p. ej. parlante apagado)
            return
        self._source_cache = None  # la información pudo caducar: la próxima vez se extrae de nuevo
        if self._retries < self._settings.music_stream_retries:
            # Corte de red o fallo pasajero de YouTube: se vuelve a intentar desde donde iba.
            self._retries += 1
            logger.info("Reintentando la pista %s desde %ss", track.video_id, int(self._position))
            return
        await self._fail_track(track, generation, f"FFmpeg {playback.returncode}, yt-dlp {downloader}")

    def _record_failure(self, generation: int, message: str) -> None:
        self._failures[generation] = message
        for old in list(self._failures)[:-FAILURES_KEPT]:
            del self._failures[old]
        self._bump()

    async def _fail_output(self, exc: OutputUnavailableError, generation: int) -> None:
        """Sin salida: se pausa (no se pierde la cola) hasta que el usuario reanude o cambie de salida."""
        logger.warning("Salida de audio %s no disponible: %s", exc.mode, exc.reason)
        async with self._lock:
            if generation == self._player.generation:
                self._error = f"sin salida de audio: {exc.reason}"
                self._player.pause()
                self._record_failure(generation, self._error)

    async def _fail_track(self, track: Track, generation: int, reason: str) -> None:
        """La pista no se pudo reproducir: se salta a la siguiente."""
        logger.warning("No se pudo reproducir %s: %s", track.video_id, reason)
        async with self._lock:
            if generation == self._player.generation:
                message = f"no se pudo reproducir «{track.title}»"
                self._record_failure(generation, message)
                self._advance()
                self._error = message

    async def close(self) -> None:
        """Al apagar: detiene el bucle, sus procesos y borra la información guardada."""
        self._closed = True
        self._player.stop()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        close = getattr(self._extractor, "close", None)
        if callable(close):
            close()
