"""Comandos de música para el asistente (Lumi).

Flujo: el servidor de Lumia añade estas herramientas al catálogo de /select (si el usuario tiene el
permiso), el modelo propone una, y el servidor la ejecuta llamando a POST /music/commands. Aquí se
validan los argumentos (forma cerrada), se comprueba el permiso local y se responde con texto
determinista, igual que las herramientas del servidor: el modelo nunca redacta la respuesta.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.schemas.music import DeviceOut, MusicStateOut, MusicToolSpec, TrackOut
from app.schemas.select import SelectArg
from app.services.music.controller import MusicController, MusicSnapshot, NothingToPlayError
from app.services.music.queue import QueueFullError
from app.services.music.types import Track

logger = logging.getLogger(__name__)

MODULE = "musica"
QUERY_MAX_CHARS = 200

_QUERY_ARG = {"name": "query", "kind": "str", "description": "canción o artista tal como lo dice el usuario"}

# Catálogo: name, description, args, permission. "read" no cambia nada; "control" cambia lo que suena.
_CATALOG: tuple[tuple[str, str, list[dict[str, Any]], str], ...] = (
    ("music.play", "Poner o reproducir música: una canción o un artista, o reanudar si no dice cuál",
     [_QUERY_ARG], "music.control"),
    ("music.search", "Buscar canciones sin reproducirlas", [{**_QUERY_ARG, "required": True}], "music.read"),
    ("music.pause", "Pausar la música", [], "music.control"),
    ("music.resume", "Reanudar la música en pausa", [], "music.control"),
    ("music.next", "Pasar a la siguiente canción", [], "music.control"),
    ("music.previous", "Volver a la canción anterior", [], "music.control"),
    ("music.current", "Qué canción está sonando ahora", [], "music.read"),
    ("music.device_status", "Estado del altavoz de música (ESP32 y Bluetooth)", [], "music.read"),
)

TOOL_PERMISSIONS = {name: permission for name, _, _, permission in _CATALOG}
TOOL_ARGS = {name: {a["name"] for a in args} for name, _, args, _ in _CATALOG}
REQUIRED_ARGS = {name: {a["name"] for a in args if a.get("required")} for name, _, args, _ in _CATALOG}


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
        duration_seconds=track.duration_seconds,
    )


def to_state(snap: MusicSnapshot) -> MusicStateOut:
    report = snap.device
    device = DeviceOut(
        connected=snap.device_connected,
        last_seen_seconds=snap.device_last_seen_seconds,
        reconnects=snap.device_reconnects,
        **(
            {
                "device_id": report.device_id,
                "bluetooth_connected": report.bluetooth_connected,
                "bluetooth_sink": report.bluetooth_sink,
                "wifi_rssi": report.wifi_rssi,
                "buffer_ms": report.buffer_ms,
                "free_heap": report.free_heap,
                "firmware": report.firmware,
            }
            if report
            else {}
        ),
    )
    return MusicStateOut(
        status=snap.status,
        generation=snap.generation,
        current=to_track(snap.current) if snap.current else None,
        upcoming=[to_track(t) for t in snap.upcoming],
        streaming=snap.streaming,
        device=device,
    )


# --- Texto ------------------------------------------------------------------------------------


def describe(track: Track) -> str:
    text = f"«{track.title}»"
    if track.artist:
        text += f" de {track.artist}"
    return text


def _duration(seconds: int | None) -> str:
    return f" ({seconds // 60}:{seconds % 60:02d})" if seconds else ""


def _speaker_note(snap: MusicSnapshot) -> str:
    if not snap.device_connected:
        return " El altavoz no está conectado; sonará cuando se conecte."
    if snap.device and not snap.device.bluetooth_connected:
        return " El ESP32 está conectado, pero sin altavoz Bluetooth."
    return ""


def device_text(snap: MusicSnapshot) -> str:
    report = snap.device
    if report is None:
        return "El altavoz de música nunca se ha conectado."
    if not snap.device_connected:
        return f"El altavoz no está conectado (último contacto hace {int(snap.device_last_seen_seconds or 0)} s)."
    bluetooth = (
        f"Bluetooth conectado a {report.bluetooth_sink}" if report.bluetooth_connected and report.bluetooth_sink
        else "Bluetooth conectado" if report.bluetooth_connected
        else "sin altavoz Bluetooth"
    )
    wifi = f", Wi-Fi {report.wifi_rssi} dBm" if report.wifi_rssi is not None else ""
    return f"El altavoz está conectado ({bluetooth}{wifi})."


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


def validate_args(tool: str, args: dict[str, Any]) -> dict[str, str]:
    """Forma cerrada: solo los argumentos del catálogo, como texto acotado y no vacío."""
    if tool not in TOOL_PERMISSIONS:
        raise InvalidCommandError(f"herramienta desconocida: {tool}")
    unknown = set(args) - TOOL_ARGS[tool]
    if unknown:
        raise InvalidCommandError(f"argumentos no permitidos: {', '.join(sorted(unknown))}")
    clean: dict[str, str] = {}
    for name, value in args.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise InvalidCommandError(f"{name} debe ser texto")
        text = " ".join(value.split())[:QUERY_MAX_CHARS]
        if text:
            clean[name] = text
    missing = REQUIRED_ARGS[tool] - set(clean)
    if missing:
        raise InvalidCommandError(f"falta el argumento: {', '.join(sorted(missing))}")
    return clean


def can_control(settings: Settings, user_ref: str) -> bool:
    allowed = settings.music_control_user_refs
    return not allowed or user_ref in allowed


class MusicCommands:
    def __init__(self, settings: Settings, controller: MusicController) -> None:
        self._settings = settings
        self._controller = controller
        self._handlers: dict[str, Callable[[dict[str, str]], Awaitable[CommandResult]]] = {
            "music.play": self._play,
            "music.search": self._search,
            "music.pause": self._pause,
            "music.resume": self._resume,
            "music.next": self._next,
            "music.previous": self._previous,
            "music.current": self._current,
            "music.device_status": self._device_status,
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
        except UpstreamServiceError as exc:
            logger.warning("Comando %s falló: %s", tool, exc)
            return CommandResult(tool, False, "No pude acceder al servicio de música ahora mismo.", "upstream")

    async def _play(self, args: dict[str, str]) -> CommandResult:
        track = await self._controller.play(query=args.get("query"))
        note = _speaker_note(self._controller.snapshot())
        return CommandResult("music.play", True, f"Reproduciendo {describe(track)}.{note}", tracks=[track])

    async def _search(self, args: dict[str, str]) -> CommandResult:
        tracks = await self._controller.search(args["query"])
        if not tracks:
            return CommandResult("music.search", False, "No encontré canciones con esa búsqueda.", "not_found")
        lines = [f"{i}. {describe(t)}{_duration(t.duration_seconds)}" for i, t in enumerate(tracks, 1)]
        return CommandResult("music.search", True, "Encontré estas canciones:\n" + "\n".join(lines), tracks=tracks)

    async def _pause(self, _: dict[str, str]) -> CommandResult:
        if await self._controller.pause():
            return CommandResult("music.pause", True, "Música en pausa.")
        return CommandResult("music.pause", False, "No hay nada sonando.", "nothing_playing")

    async def _resume(self, _: dict[str, str]) -> CommandResult:
        if await self._controller.resume():
            current = self._controller.snapshot().current
            return CommandResult("music.resume", True, f"Reanudando {describe(current)}." if current else "Reanudando.")
        return CommandResult("music.resume", False, "La música no está en pausa.", "nothing_playing")

    async def _next(self, _: dict[str, str]) -> CommandResult:
        track = await self._controller.next()
        if track is None:
            return CommandResult("music.next", False, "No hay más canciones en la cola.", "not_found")
        return CommandResult("music.next", True, f"Siguiente: {describe(track)}.", tracks=[track])

    async def _previous(self, _: dict[str, str]) -> CommandResult:
        track = await self._controller.previous()
        if track is None:
            return CommandResult("music.previous", False, "No hay canción anterior.", "not_found")
        return CommandResult("music.previous", True, f"Volviendo a {describe(track)}.", tracks=[track])

    async def _current(self, _: dict[str, str]) -> CommandResult:
        snap = self._controller.snapshot()
        if snap.current is None or snap.status == "idle":
            return CommandResult("music.current", True, "No está sonando nada.")
        prefix = "En pausa" if snap.status == "paused" else "Está sonando"
        return CommandResult("music.current", True, f"{prefix}: {describe(snap.current)}.", tracks=[snap.current])

    async def _device_status(self, _: dict[str, str]) -> CommandResult:
        return CommandResult("music.device_status", True, device_text(self._controller.snapshot()))
