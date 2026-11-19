"""Coordinación del módulo de música: única puerta de entrada para la API y los comandos.

Una instancia por proceso (ver main.lifespan). Las operaciones que cambian la cola o el estado se
serializan con un lock; la búsqueda y la extracción (lentas) ocurren fuera de él.

La música suena en el propio servidor. Un bucle en segundo plano (`_run`) mira el estado y mantiene un
tramo de reproducción (yt-dlp → FFmpeg → salida de audio, ver stream.py) con la pista actual: si el
estado cambia (pausa, otra pista, otra salida, otra posición) termina ese tramo y, si toca, abre otro.
Al acabar una pista pasa a la siguiente.

Nada se da por «sonando» hasta que FFmpeg informa de audio reproducido (`Playback.started`): mientras
tanto el estado es `loading`. Los comandos esperan esa confirmación (`wait_until_playing`).

Al terminar una pista decide el propio servidor (sin depender del navegador): repetir la canción,
seguir con la cola, volver al principio (repetir la cola) o, con la reproducción automática, añadir
una canción relacionada real (Mix de YouTube) que no haya sonado hace poco.

Cada cambio visible sube `version` y despierta a `wait_change` (eventos SSE del reproductor).
"""

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
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
# off: la cola termina · one: repite la canción actual · all: al acabar la cola vuelve a empezar.
RepeatMode = Literal["off", "one", "all"]
REPEAT_MODES: tuple[RepeatMode, ...] = ("off", "one", "all")
# Candidatas que la reproducción automática intenta resolver antes de rendirse.
AUTOPLAY_ATTEMPTS = 3
RECOMMENDATIONS_FETCH = 25
RECOMMENDATIONS_CACHE_ITEMS = 20


class NothingToPlayError(Exception):
    pass


class AlreadyQueuedError(Exception):
    def __init__(self, track: Track) -> None:
        super().__init__(track.video_id)
        self.track = track


_BRACKETS_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NON_WORD_RE = re.compile(r"[^\w]+")


def title_key(track: Track) -> str:
    """Título normalizado para reconocer la misma canción en otra subida («(Official Video)», artista…)."""
    text = _BRACKETS_RE.sub(" ", track.title.lower())
    if track.artist:
        text = text.replace(track.artist.lower(), " ")
    return " ".join(_NON_WORD_RE.sub(" ", text).split())


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
    shuffle: bool = False
    repeat_mode: RepeatMode = "off"
    autoplay: bool = False
    # Sube con cada cambio visible del estado (para no reenviar lo mismo).
    version: int = 0


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
        self._version = 0
        self._state_event = asyncio.Event()
        self._player = PlayerState(on_change=self._publish)
        self._shuffle = False
        self._repeat: RepeatMode = "off"
        self._autoplay = settings.music_autoplay_default
        # Canciones seguidas que añadió la reproducción automática sin una orden del usuario.
        self._autoplay_streak = 0
        # Últimas canciones que sonaron de verdad (la más reciente al final).
        self._history: deque[Track] = deque(maxlen=settings.music_history_size)
        self._recommendations: dict[str, tuple[float, list[Track]]] = {}
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
                self._autoplay_streak = 0
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

    async def enqueue(
        self, query: str | None = None, video_id: str | None = None, play_next: bool = False
    ) -> Track:
        """Añade a la cola (al final, o justo después de la actual con `play_next`). Sin duplicados
        entre las pendientes: AlreadyQueuedError si ya está esperando."""
        track = await self._find(query, video_id)
        async with self._lock:
            if self._queue.has_upcoming(track.video_id):
                raise AlreadyQueuedError(track)
            self._autoplay_streak = 0
            if play_next:
                self._queue.insert_next(track)
            else:
                self._queue.add(track)
        self._publish()
        return track

    async def remove(self, position: int) -> Track | None:
        async with self._lock:
            track = self._queue.remove(position)
        if track:
            self._publish()
        return track

    async def move(self, position: int, to: int) -> Track | None:
        async with self._lock:
            track = self._queue.move(position, to)
        if track:
            self._publish()
        return track

    async def clear_upcoming(self) -> int:
        async with self._lock:
            removed = self._queue.clear_upcoming()
        self._publish()
        return removed

    async def set_shuffle(self, enabled: bool) -> bool:
        """Aleatorio: al activarlo se mezclan las pendientes (la actual sigue sonando)."""
        async with self._lock:
            self._shuffle = enabled
            if enabled:
                self._queue.shuffle_upcoming()
        self._publish()
        return enabled

    async def set_repeat(self, mode: RepeatMode) -> RepeatMode:
        if mode not in REPEAT_MODES:
            raise ValueError("modo de repetición no válido")
        self._repeat = mode
        self._publish()
        return mode

    async def set_autoplay(self, enabled: bool) -> bool:
        self._autoplay = enabled
        self._autoplay_streak = 0
        self._publish()
        return enabled

    async def artist_tracks(self, artist: str, limit: int) -> list[Track]:
        """Más canciones de un artista (búsqueda en YouTube), con la misma caché que las recomendaciones."""
        key = f"artist:{artist.lower()}"
        now = self._clock()
        cached = self._recommendations.get(key)
        if cached and now - cached[0] < self._settings.music_recommendations_cache_seconds:
            return cached[1][:limit]
        tracks = await self.search(artist, limit)
        self._recommendations[key] = (now, tracks)
        return tracks

    def history(self) -> list[Track]:
        """Canciones que sonaron, de la más reciente a la más antigua."""
        return list(reversed(self._history))

    async def recommendations(self, video_id: str, limit: int = 20) -> list[Track]:
        """Canciones relacionadas reales (Mix de YouTube de ese vídeo), reproducibles."""
        now = self._clock()
        cached = self._recommendations.get(video_id)
        ttl = self._settings.music_recommendations_cache_seconds
        if cached and now - cached[0] < ttl:
            return cached[1][:limit]
        tracks = await self._extractor.related(video_id, RECOMMENDATIONS_FETCH)
        tracks = [t for t in tracks if self._search.is_playable(t)]
        self._recommendations[video_id] = (now, tracks)
        for old in list(self._recommendations)[:-RECOMMENDATIONS_CACHE_ITEMS]:
            del self._recommendations[old]
        return tracks[:limit]

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
        """Pasa a la siguiente. Sin pendientes: con «repetir cola» vuelve a la primera; con la
        reproducción automática añade una relacionada. Si no, no cambia nada (la actual sigue)."""
        async with self._lock:
            self._autoplay_streak = 0
            if self._queue.upcoming:
                return self._advance()
            current = self._queue.current
            generation = self._player.generation
            if self._repeat == "all" and self._queue.restart() is not None:
                self._start_track()
                return self._queue.current
        if current is None or not self._autoplay:
            return None
        self._ensure_runner()
        return await self._autoplay_next(current, generation)

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
            self._autoplay_streak = 0
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
        self._publish()
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

    # --- Fin de pista y reproducción automática ----------------------------------------------

    async def _on_track_end(self, track: Track, generation: int) -> None:
        """La pista terminó sola: decide qué suena después según repetir y la reproducción automática."""
        async with self._lock:
            if generation != self._player.generation or self._player.status != "playing":
                return
            if self._repeat == "one":
                self._start_track()  # misma posición de la cola, generación nueva
                return
            if self._queue.upcoming:
                self._advance()
                return
            if self._repeat == "all" and self._queue.restart() is not None:
                self._start_track()
                return
            if not self._autoplay or self._autoplay_streak >= self._settings.music_autoplay_max_tracks:
                self._advance()  # fin de la cola: se detiene
                return
        if await self._autoplay_next(track, generation) is None:
            async with self._lock:
                if generation == self._player.generation and self._player.status == "playing":
                    self._advance()

    async def _autoplay_next(self, current: Track, generation: int) -> Track | None:
        """Añade una relacionada y la reproduce. None si no hay ninguna válida o algo cambió entretanto."""
        pick = await self._autoplay_pick(current)
        async with self._lock:
            if generation != self._player.generation:
                return None  # otra orden cambió la pista mientras se buscaba
            if self._queue.upcoming:
                return self._advance()  # alguien añadió algo mientras tanto: va primero
            if pick is None:
                return None
            self._queue.add(pick)
            self._autoplay_streak += 1
            if self._player.status != "playing":
                self._publish()
                return None  # en pausa: queda en la cola
            logger.info("Reproducción automática: %s", pick.video_id)
            return self._advance()

    async def _autoplay_pick(self, current: Track) -> Track | None:
        """Primera recomendación real que no sonó hace poco ni está en la cola, y que se puede resolver."""
        try:
            candidates = await self.recommendations(current.video_id)
        except (UpstreamServiceError, ValueError) as exc:
            logger.warning("Sin recomendaciones para %s: %s", current.video_id, exc)
            return None
        recent = [*self._history, *self._queue.items, current]
        seen_ids = {t.video_id for t in recent}
        seen_titles = {title_key(t) for t in recent}
        attempts = 0
        for candidate in candidates:
            if candidate.video_id in seen_ids or title_key(candidate) in seen_titles:
                continue
            attempts += 1
            try:
                # Extraer la valida (disponible, con audio) y deja su información lista para sonar.
                return await self._find(None, candidate.video_id)
            except (UpstreamServiceError, NothingToPlayError, ValueError) as exc:
                logger.info("Recomendación %s descartada: %s", candidate.video_id, exc)
                if attempts >= AUTOPLAY_ATTEMPTS:
                    break
        return None

    def _remember(self, track: Track) -> None:
        if not self._history or self._history[-1].video_id != track.video_id:
            self._history.append(track)

    # --- Confirmación ----------------------------------------------------------------------

    def _publish(self) -> None:
        """Algo visible cambió: sube la versión y despierta a los suscriptores de eventos."""
        self._version += 1
        event, self._state_event = self._state_event, asyncio.Event()
        event.set()

    @property
    def version(self) -> int:
        return self._version

    async def wait_change(self, version: int, timeout: float) -> int:
        """Espera a que la versión cambie respecto de `version` (o a que pase `timeout`)."""
        if self._version == version:
            event = self._state_event
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)
        return self._version

    def _bump(self) -> None:
        """Despierta a quien espera una confirmación o un fallo."""
        activity, self._activity = self._activity, asyncio.Event()
        activity.set()
        self._publish()

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
            shuffle=self._shuffle,
            repeat_mode=self._repeat,
            autoplay=self._autoplay,
            version=self._version,
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
                self._remember(track)
                self._bump()
                await asyncio.wait({ended, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            started.cancel()
            await self._ffmpeg.stop(playback)
            await asyncio.gather(ended, return_exceptions=True)
            self._playback = None
            self._sink = None
            self._publish()
            if generation == self._position_generation:
                if self._seek_to is not None:
                    self._position, self._seek_to = self._seek_to, None
                else:
                    self._position = playback.position

        if changed.is_set():
            return  # pausa, otra pista, otra salida u otra posición: lo decide la siguiente vuelta
        downloader = await playback.downloader_code()
        if playback.returncode == 0 and downloader in (0, None):
            await self._on_track_end(track, generation)  # pista terminada
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
