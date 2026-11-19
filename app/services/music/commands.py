"""Comandos de música para el asistente (Lumi) y para el reproductor del chat de Lumia Admin.

Flujo: el servidor de Lumia añade estas herramientas al catálogo de /select (si el usuario tiene el
permiso), el modelo propone una, y el servidor la ejecuta llamando a POST /music/commands. Los botones
del reproductor de Lumia Admin usan el mismo endpoint (a través del servidor). Aquí se validan los
argumentos (forma cerrada), se comprueba el permiso local y se responde con texto determinista: el
modelo nunca redacta la respuesta.

Nada se anuncia como «sonando» sin que el reproductor lo confirme (ver MusicController.wait_until_playing).
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.schemas.music import MusicStateOut, MusicToolSpec, OutputOut, TrackOut
from app.schemas.select import SelectArg
from app.services.music.controller import (
    REPEAT_MODES,
    AlreadyQueuedError,
    MusicController,
    MusicSnapshot,
    NothingToPlayError,
)
from app.services.music.output import OUTPUT_MODES, OutputMode, OutputUnavailableError
from app.services.music.queue import QueueFullError
from app.services.music.types import SOURCE, Track, is_video_id

logger = logging.getLogger(__name__)

MODULE = "musica"
QUERY_MAX_CHARS = 200
VOLUME_STEP = 10

_QUERY_ARG = {"name": "query", "kind": "str", "description": "canción o artista tal como lo dice el usuario"}
# ID de YouTube (11 caracteres): lo envían los botones del centro de música, nunca una URL.
_VIDEO_ARG = {"name": "video_id", "kind": "str", "description": "ID de YouTube de un resultado ya mostrado"}
_SWITCH = ["on", "off"]
_POSITION_ARG = {"name": "position", "kind": "int", "required": True, "description": "1 = la siguiente de la cola"}
_MODE_ARG = {
    "name": "mode",
    "kind": list(OUTPUT_MODES),
    "required": True,
    "description": "aux = cable o jack; bluetooth = parlante inalámbrico",
}

# Catálogo: name, description, args, permission. "read" no cambia nada; "control" cambia lo que suena.
_CATALOG: tuple[tuple[str, str, list[dict[str, Any]], str], ...] = (
    ("music.play", "Poner o reproducir música: una canción o un artista, o reanudar si no dice cuál",
     [_QUERY_ARG, _VIDEO_ARG], "music.control"),
    ("music.search", "Buscar canciones sin reproducirlas", [{**_QUERY_ARG, "required": True}], "music.read"),
    ("music.enqueue", "Añadir una canción a la cola sin cortar la que suena",
     [_QUERY_ARG, _VIDEO_ARG], "music.control"),
    ("music.play_next", "Poner una canción justo después de la que suena",
     [_QUERY_ARG, _VIDEO_ARG], "music.control"),
    ("music.queue", "Ver las canciones que hay en la cola", [], "music.read"),
    ("music.jump", "Reproducir una canción concreta de la cola", [_POSITION_ARG], "music.control"),
    ("music.remove", "Quitar una canción de la cola", [_POSITION_ARG], "music.control"),
    ("music.move", "Cambiar el orden de una canción de la cola",
     [_POSITION_ARG, {"name": "to", "kind": "int", "required": True, "description": "nueva posición (1 = la siguiente)"}],
     "music.control"),
    ("music.clear_queue", "Vaciar la cola (la canción actual sigue sonando)", [], "music.control"),
    ("music.shuffle", "Activar o desactivar el modo aleatorio",
     [{"name": "state", "kind": _SWITCH, "required": True, "description": "on = activar, off = desactivar"}],
     "music.control"),
    ("music.repeat", "Repetir la canción, repetir la cola o no repetir",
     [{"name": "repeat", "kind": list(REPEAT_MODES), "required": True,
       "description": "one = esta canción, all = toda la cola, off = no repetir"}],
     "music.control"),
    ("music.autoplay", "Activar o desactivar la reproducción automática de canciones relacionadas",
     [{"name": "state", "kind": _SWITCH, "required": True, "description": "on = activar, off = desactivar"}],
     "music.control"),
    ("music.recommend", "Recomendar canciones parecidas a la que suena", [], "music.read"),
    ("music.history", "Ver las últimas canciones que sonaron", [], "music.read"),
    ("music.pause", "Pausar la música", [], "music.control"),
    ("music.resume", "Reanudar la música en pausa", [], "music.control"),
    ("music.stop", "Detener la música", [], "music.control"),
    ("music.next", "Pasar a la siguiente canción", [], "music.control"),
    ("music.previous", "Volver a la canción anterior", [], "music.control"),
    ("music.volume", "Cambiar el volumen de la música: a un nivel de 0 a 100, o subirlo o bajarlo",
     [
         {"name": "level", "kind": "int", "description": "nivel de 0 a 100"},
         {"name": "change", "kind": ["up", "down"], "description": "up = subir, down = bajar"},
     ],
     "music.control"),
    ("music.seek", "Ir a un momento de la canción que suena",
     [{"name": "position", "kind": "int", "required": True, "description": "segundo de la canción"}],
     "music.control"),
    ("music.current", "Qué canción está sonando ahora", [], "music.read"),
    ("music.output", "Cambiar por dónde suena la música: cable auxiliar o parlante Bluetooth",
     [_MODE_ARG], "music.control"),
    ("music.output_status", "Por dónde está sonando la música y si el parlante está conectado", [], "music.read"),
)

TOOL_PERMISSIONS = {name: permission for name, _, _, permission in _CATALOG}
TOOL_ARGS = {name: {a["name"]: a for a in args} for name, _, args, _ in _CATALOG}
REQUIRED_ARGS = {name: {a["name"] for a in args if a.get("required")} for name, _, args, _ in _CATALOG}
# Rangos de los argumentos enteros.
INT_RANGES = {"level": (0, 100), "position": (0, 24 * 3600), "to": (1, 500)}
# Herramientas que necesitan una canción: por búsqueda (query) o por ID (video_id).
NEEDS_TARGET = {"music.enqueue", "music.play_next"}
RECOMMEND_SHOWN = 5

OUTPUT_LABELS: dict[OutputMode, str] = {"aux": "el cable auxiliar", "bluetooth": "el parlante Bluetooth"}

# Lo que el modelo o el usuario pueden decir para cada salida.
_MODE_ALIASES: dict[str, OutputMode] = {
    "aux": "aux", "auxiliar": "aux", "cable": "aux", "jack": "aux", "linea": "aux", "línea": "aux",
    "bluetooth": "bluetooth", "bt": "bluetooth", "parlante": "bluetooth", "inalambrico": "bluetooth",
    "inalámbrico": "bluetooth",
}


def parse_mode(value: str) -> OutputMode | None:
    text = value.strip().lower()
    if text in _MODE_ALIASES:
        return _MODE_ALIASES[text]
    if "blue" in text:
        return "bluetooth"
    return None


def tool_specs() -> list[MusicToolSpec]:
    return [
        MusicToolSpec(
            name=name,
            description=description,
            args=[SelectArg(**a) for a in args],
            module=MODULE,
            permission=permission,  # type: ignore[arg-type]
        )
        for name, description, args, permission in _CATALOG
    ]


# --- Conversión a esquemas ---------------------------------------------------------------------


def to_track(track: Track) -> TrackOut:
    return TrackOut(
        video_id=track.video_id,
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_seconds=track.duration_seconds,
        thumbnail_url=track.thumbnail,
        url=track.url,
        source=SOURCE,
    )


def to_state(snap: MusicSnapshot) -> MusicStateOut:
    return MusicStateOut(
        shuffle=snap.shuffle,
        repeat_mode=snap.repeat_mode,
        autoplay=snap.autoplay,
        version=snap.version,
        updated_at=int(time.time() * 1000),
        status=snap.status,
        generation=snap.generation,
        current=to_track(snap.current) if snap.current else None,
        upcoming=[to_track(t) for t in snap.upcoming],
        position_seconds=snap.position_seconds,
        duration_seconds=snap.current.duration_seconds if snap.current else None,
        volume=snap.volume,
        output=OutputOut(mode=snap.output_mode, sink=snap.sink),
        error=snap.error,
    )


# --- Texto ------------------------------------------------------------------------------------


def describe(track: Track) -> str:
    text = f"«{track.title}»"
    if track.artist:
        text += f" de {track.artist}"
    return text


def clock(seconds: int | None) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _duration(seconds: int | None) -> str:
    return f" ({clock(seconds)})" if seconds else ""


def unavailable_text(exc: OutputUnavailableError) -> str:
    return f"No puedo sonar por {OUTPUT_LABELS[exc.mode]}: {exc.reason}."


def output_text(snap: MusicSnapshot) -> str:
    label = OUTPUT_LABELS[snap.output_mode]
    if snap.sink:
        text = f"La música está sonando por {label}."
    elif snap.status == "paused":
        text = f"La música está en pausa; la salida elegida es {label}."
    else:
        text = f"No suena nada ahora; la salida elegida es {label}."
    if snap.error:
        text += f" Último problema: {snap.error}."
    return text


# --- Ejecución --------------------------------------------------------------------------------


class InvalidCommandError(ValueError):
    pass


@dataclass
class CommandResult:
    tool: str
    ok: bool
    text: str
    error: str | None = None
    tracks: list[Track] = field(default_factory=list)


def _int_arg(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidCommandError(f"{name} debe ser un número")
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise InvalidCommandError(f"{name} debe ser un número")
    low, high = INT_RANGES.get(name, (0, 10_000))
    if not low <= value <= high:
        raise InvalidCommandError(f"{name} fuera de rango ({low}-{high})")
    return value


def validate_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Forma cerrada: solo los argumentos del catálogo, con su tipo (texto acotado, entero o lista)."""
    if tool not in TOOL_PERMISSIONS:
        raise InvalidCommandError(f"herramienta desconocida: {tool}")
    specs = TOOL_ARGS[tool]
    unknown = set(args) - set(specs)
    if unknown:
        raise InvalidCommandError(f"argumentos no permitidos: {', '.join(sorted(unknown))}")
    clean: dict[str, Any] = {}
    for name, value in args.items():
        if value is None:
            continue
        kind = specs[name].get("kind", "str")
        if kind == "int":
            clean[name] = _int_arg(name, value)
            continue
        if not isinstance(value, str):
            raise InvalidCommandError(f"{name} debe ser texto")
        if name == "video_id":
            if not is_video_id(value):
                raise InvalidCommandError("video_id inválido")
            clean[name] = value
            continue
        text = " ".join(value.split())[:QUERY_MAX_CHARS]
        if not text:
            continue
        if isinstance(kind, list) and name != "mode" and text not in kind:
            raise InvalidCommandError(f"{name} debe ser uno de: {', '.join(kind)}")
        clean[name] = text
    missing = REQUIRED_ARGS[tool] - set(clean)
    if missing:
        raise InvalidCommandError(f"falta el argumento: {', '.join(sorted(missing))}")
    if "query" in clean and "video_id" in clean:
        raise InvalidCommandError("indica query o video_id, no los dos")
    if tool in NEEDS_TARGET and "query" not in clean and "video_id" not in clean:
        raise InvalidCommandError("falta el argumento: query o video_id")
    return clean


def can_control(settings: Settings, user_ref: str) -> bool:
    allowed = settings.music_control_user_refs
    return not allowed or user_ref in allowed


class MusicCommands:
    def __init__(self, settings: Settings, controller: MusicController) -> None:
        self._settings = settings
        self._controller = controller
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[CommandResult]]] = {
            "music.play": self._play,
            "music.search": self._search,
            "music.enqueue": self._enqueue,
            "music.play_next": self._play_next,
            "music.queue": self._queue,
            "music.jump": self._jump,
            "music.remove": self._remove,
            "music.move": self._move,
            "music.clear_queue": self._clear_queue,
            "music.shuffle": self._shuffle,
            "music.repeat": self._repeat,
            "music.autoplay": self._autoplay,
            "music.recommend": self._recommend,
            "music.history": self._history,
            "music.pause": self._pause,
            "music.resume": self._resume,
            "music.stop": self._stop,
            "music.next": self._next,
            "music.previous": self._previous,
            "music.volume": self._volume,
            "music.seek": self._seek,
            "music.current": self._current,
            "music.output": self._output,
            "music.output_status": self._output_status,
        }

    async def execute(self, user_ref: str, tool: str, args: dict[str, Any]) -> CommandResult:
        clean = validate_args(tool, args)  # InvalidCommandError -> 422 en la API
        if TOOL_PERMISSIONS[tool] == "music.control" and not can_control(self._settings, user_ref):
            return CommandResult(tool, False, "No tienes permiso para controlar la música.", "forbidden")
        logger.info("Comando de música %s de %s", tool, user_ref)
        try:
            return await self._handlers[tool](clean)
        except NothingToPlayError:
            return CommandResult(tool, False, "No encontré ninguna canción para reproducir.", "not_found")
        except QueueFullError:
            return CommandResult(tool, False, "La cola de música está llena.", "queue_full")
        except AlreadyQueuedError as exc:
            return CommandResult(tool, False, f"{describe(exc.track)} ya está en la cola.", "duplicate", [exc.track])
        except OutputUnavailableError as exc:
            return CommandResult(tool, False, unavailable_text(exc), "output_unavailable")
        except UpstreamServiceError as exc:
            logger.warning("Comando %s falló: %s", tool, exc)
            return CommandResult(tool, False, "No pude acceder al servicio de música ahora mismo.", "upstream")

    # --- Confirmación de que suena ---------------------------------------------------------

    async def _confirm(self, tool: str, track: Track, generation: int, segment: int, verb: str) -> CommandResult:
        """Responde según lo que de verdad pasó: sonando, todavía cargando o fallo."""
        outcome = await self._controller.wait_until_playing(
            generation, segment, self._settings.music_start_wait_seconds
        )
        if outcome == "playing":
            return CommandResult(tool, True, f"{verb} {describe(track)}.", tracks=[track])
        if outcome == "failed":
            reason = self._controller.failure(generation) or "error de reproducción"
            return CommandResult(tool, False, f"No pude reproducir {describe(track)}: {reason}.", "playback_failed", [track])
        if outcome == "loading":
            return CommandResult(
                tool, True, f"Cargando {describe(track)}; empezará a sonar en unos segundos.", tracks=[track]
            )
        return CommandResult(tool, True, f"La reproducción cambió antes de empezar {describe(track)}.", tracks=[track])

    async def _start_and_confirm(
        self, tool: str, action: Callable[[], Awaitable[Track | None]], verb: str
    ) -> Track | CommandResult | None:
        segment = self._controller.segment
        track = await action()
        if track is None:
            return None
        # Se lee justo después de la orden, sin ceder el control: es la generación que esta abrió.
        generation = self._controller.last_started_generation
        return await self._confirm(tool, track, generation, segment, verb)

    # --- Comandos --------------------------------------------------------------------------

    async def _play(self, args: dict[str, Any]) -> CommandResult:
        query = args.get("query")
        video_id = args.get("video_id")
        snap = self._controller.snapshot()
        if not query and not video_id and snap.status == "playing" and snap.current:
            return CommandResult("music.play", True, f"Ya está sonando {describe(snap.current)}.", tracks=[snap.current])
        result = await self._start_and_confirm(
            "music.play", lambda: self._controller.play(query=query, video_id=video_id), "Reproduciendo"
        )
        assert isinstance(result, CommandResult)
        return result

    async def _search(self, args: dict[str, Any]) -> CommandResult:
        tracks = await self._controller.search(args["query"])
        if not tracks:
            return CommandResult("music.search", False, "No encontré canciones con esa búsqueda.", "not_found")
        lines = [f"{i}. {describe(t)}{_duration(t.duration_seconds)}" for i, t in enumerate(tracks, 1)]
        return CommandResult("music.search", True, "Encontré estas canciones:\n" + "\n".join(lines), tracks=tracks)

    async def _enqueue(self, args: dict[str, Any]) -> CommandResult:
        track = await self._controller.enqueue(query=args.get("query"), video_id=args.get("video_id"))
        position = len(self._controller.snapshot().upcoming)
        return CommandResult(
            "music.enqueue", True, f"Añadí {describe(track)} a la cola (posición {position}).", tracks=[track]
        )

    async def _play_next(self, args: dict[str, Any]) -> CommandResult:
        track = await self._controller.enqueue(
            query=args.get("query"), video_id=args.get("video_id"), play_next=True
        )
        return CommandResult("music.play_next", True, f"{describe(track)} sonará a continuación.", tracks=[track])

    async def _remove(self, args: dict[str, Any]) -> CommandResult:
        track = await self._controller.remove(args["position"])
        if track is None:
            return CommandResult("music.remove", False, "No hay ninguna canción en esa posición de la cola.", "not_found")
        return CommandResult("music.remove", True, f"Quité {describe(track)} de la cola.", tracks=[track])

    async def _move(self, args: dict[str, Any]) -> CommandResult:
        track = await self._controller.move(args["position"], args["to"])
        if track is None:
            return CommandResult("music.move", False, "No hay ninguna canción en esa posición de la cola.", "not_found")
        return CommandResult("music.move", True, f"{describe(track)} ahora es la número {args['to']} de la cola.", tracks=[track])

    async def _clear_queue(self, _: dict[str, Any]) -> CommandResult:
        removed = await self._controller.clear_upcoming()
        if removed == 0:
            return CommandResult("music.clear_queue", True, "La cola ya estaba vacía.")
        return CommandResult("music.clear_queue", True, f"Vacié la cola ({removed} canciones).")

    async def _shuffle(self, args: dict[str, Any]) -> CommandResult:
        enabled = await self._controller.set_shuffle(args["state"] == "on")
        return CommandResult("music.shuffle", True, "Modo aleatorio activado." if enabled else "Modo aleatorio desactivado.")

    async def _repeat(self, args: dict[str, Any]) -> CommandResult:
        mode = await self._controller.set_repeat(args["repeat"])
        text = {"off": "Repetición desactivada.", "one": "Repetiré esta canción.", "all": "Repetiré toda la cola."}[mode]
        return CommandResult("music.repeat", True, text)

    async def _autoplay(self, args: dict[str, Any]) -> CommandResult:
        enabled = await self._controller.set_autoplay(args["state"] == "on")
        text = (
            "Reproducción automática activada: al acabar la cola pondré canciones relacionadas."
            if enabled
            else "Reproducción automática desactivada."
        )
        return CommandResult("music.autoplay", True, text)

    async def _recommend(self, _: dict[str, Any]) -> CommandResult:
        current = self._controller.snapshot().current
        if current is None:
            return CommandResult("music.recommend", False, "Pon una canción y te recomiendo otras parecidas.", "nothing_playing")
        tracks = (await self._controller.recommendations(current.video_id))[:RECOMMEND_SHOWN]
        if not tracks:
            return CommandResult("music.recommend", False, "No encontré canciones parecidas ahora mismo.", "not_found")
        lines = [f"{i}. {describe(t)}{_duration(t.duration_seconds)}" for i, t in enumerate(tracks, 1)]
        head = f"Parecidas a {describe(current)}:\n"
        return CommandResult("music.recommend", True, head + "\n".join(lines), tracks=tracks)

    async def _history(self, _: dict[str, Any]) -> CommandResult:
        tracks = self._controller.history()[:10]
        if not tracks:
            return CommandResult("music.history", True, "Todavía no ha sonado ninguna canción.")
        lines = [f"{i}. {describe(t)}" for i, t in enumerate(tracks, 1)]
        return CommandResult("music.history", True, "Sonaron hace poco:\n" + "\n".join(lines), tracks=tracks)

    async def _queue(self, _: dict[str, Any]) -> CommandResult:
        snap = self._controller.snapshot()
        if not snap.upcoming:
            text = "La cola está vacía."
            if snap.current:
                text = f"Ahora: {describe(snap.current)}. No hay más canciones en la cola."
            return CommandResult("music.queue", True, text)
        lines = [f"{i}. {describe(t)}{_duration(t.duration_seconds)}" for i, t in enumerate(snap.upcoming, 1)]
        head = f"Ahora: {describe(snap.current)}.\n" if snap.current else ""
        return CommandResult("music.queue", True, f"{head}En la cola:\n" + "\n".join(lines), tracks=snap.upcoming)

    async def _jump(self, args: dict[str, Any]) -> CommandResult:
        result = await self._start_and_confirm(
            "music.jump", lambda: self._controller.jump(args["position"]), "Reproduciendo"
        )
        if result is None:
            return CommandResult("music.jump", False, "No hay ninguna canción en esa posición de la cola.", "not_found")
        assert isinstance(result, CommandResult)
        return result

    async def _pause(self, _: dict[str, Any]) -> CommandResult:
        if await self._controller.pause():
            return CommandResult("music.pause", True, "Música en pausa.")
        return CommandResult("music.pause", False, "No hay nada sonando.", "nothing_playing")

    async def _resume(self, _: dict[str, Any]) -> CommandResult:
        current = self._controller.snapshot().current
        segment = self._controller.segment
        if not await self._controller.resume() or current is None:
            return CommandResult("music.resume", False, "La música no está en pausa.", "nothing_playing")
        return await self._confirm(
            "music.resume", current, self._controller.snapshot().generation, segment, "Reanudando"
        )

    async def _stop(self, _: dict[str, Any]) -> CommandResult:
        if await self._controller.stop():
            return CommandResult("music.stop", True, "Música detenida.")
        return CommandResult("music.stop", False, "No hay nada sonando.", "nothing_playing")

    async def _next(self, _: dict[str, Any]) -> CommandResult:
        result = await self._start_and_confirm("music.next", self._controller.next, "Siguiente:")
        if result is None:
            return CommandResult("music.next", False, "No hay más canciones en la cola.", "not_found")
        assert isinstance(result, CommandResult)
        return result

    async def _previous(self, _: dict[str, Any]) -> CommandResult:
        result = await self._start_and_confirm("music.previous", self._controller.previous, "Volviendo a")
        if result is None:
            return CommandResult("music.previous", False, "No hay canción anterior.", "not_found")
        assert isinstance(result, CommandResult)
        return result

    async def _volume(self, args: dict[str, Any]) -> CommandResult:
        if "level" in args:
            target = args["level"]
        elif args.get("change") in ("up", "down"):
            step = VOLUME_STEP if args["change"] == "up" else -VOLUME_STEP
            target = self._controller.volume + step
        else:
            return CommandResult("music.volume", True, f"El volumen está al {self._controller.volume} %.")
        level = await self._controller.set_volume(target)
        return CommandResult("music.volume", True, f"Volumen al {level} %.")

    async def _seek(self, args: dict[str, Any]) -> CommandResult:
        position = await self._controller.seek(args["position"])
        if position is None:
            return CommandResult("music.seek", False, "No hay ninguna canción sonando ni en pausa.", "nothing_playing")
        return CommandResult("music.seek", True, f"Listo, voy al {clock(int(position))}.")

    async def _current(self, _: dict[str, Any]) -> CommandResult:
        snap = self._controller.snapshot()
        track = snap.current
        if track is None or snap.status in ("idle", "stopped", "error"):
            return CommandResult("music.current", True, "No está sonando nada.")
        if snap.status == "loading":
            return CommandResult("music.current", True, f"Cargando {describe(track)}.", tracks=[track])
        prefix = "En pausa" if snap.status == "paused" else "Está sonando"
        progress = f" ({clock(snap.position_seconds)} de {clock(track.duration_seconds)})" if track.duration_seconds else ""
        return CommandResult("music.current", True, f"{prefix}: {describe(track)}{progress}.", tracks=[track])

    async def _output(self, args: dict[str, Any]) -> CommandResult:
        mode = parse_mode(args["mode"])
        if mode is None:
            return CommandResult("music.output", False, "Puedo sonar por el cable auxiliar o por Bluetooth.", "invalid_output")
        await self._controller.set_output(mode)
        return CommandResult("music.output", True, f"Listo, la música sonará por {OUTPUT_LABELS[mode]}.")

    async def _output_status(self, _: dict[str, Any]) -> CommandResult:
        text = output_text(self._controller.snapshot())
        try:
            await self._controller.resolve_output("bluetooth")
            text += " El parlante Bluetooth está conectado."
        except OutputUnavailableError as exc:
            text += f" Bluetooth: {exc.reason}."
        return CommandResult("music.output_status", True, text)
