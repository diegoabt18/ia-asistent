"""Salidas de audio del servidor: el jack auxiliar o un parlante Bluetooth.

El audio sale por el servidor de sonido del host (PulseAudio o PipeWire con su capa PulseAudio). Cada
salida es un "sink"; el emparejamiento Bluetooth se hace una vez en el host (ver README) y aquí solo se
elige el sink. `pactl list short sinks` devuelve una línea por sink, separada por tabuladores:
    índice  nombre  módulo  formato  estado
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from app.core.config import Settings
from app.services.music.stream import STREAM_APP

logger = logging.getLogger(__name__)

OutputMode = Literal["aux", "bluetooth"]
OUTPUT_MODES: tuple[OutputMode, ...] = ("aux", "bluetooth")

BLUETOOTH_PREFIX = "bluez_"
PACTL_TIMEOUT_SECONDS = 5.0

Spawn = Callable[..., Awaitable[Any]]


class OutputUnavailableError(Exception):
    """La salida pedida no existe ahora mismo (parlante apagado, sin servidor de sonido, etc.)."""

    def __init__(self, mode: OutputMode, reason: str) -> None:
        super().__init__(reason)
        self.mode = mode
        self.reason = reason


def parse_sinks(output: str) -> list[str]:
    names = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            names.append(parts[1].strip())
    return names


def pick_sink(mode: OutputMode, sinks: list[str], configured: str) -> str | None:
    """Sink de un modo: el configurado si existe; si no, el primero que encaje."""
    if configured:
        return configured if configured in sinks else None
    if mode == "bluetooth":
        return next((s for s in sinks if s.startswith(BLUETOOTH_PREFIX)), None)
    wired = [s for s in sinks if not s.startswith(BLUETOOTH_PREFIX)]
    return next((s for s in wired if "analog" in s), wired[0] if wired else None)


class AudioOutputs:
    def __init__(self, settings: Settings, spawn: Spawn = asyncio.create_subprocess_exec) -> None:
        self._settings = settings
        self._spawn = spawn

    async def list_sinks(self) -> list[str]:
        """Sinks disponibles. Lanza OutputUnavailableError si no se puede hablar con el servidor de sonido."""
        try:
            proc = await self._spawn(
                self._settings.music_pactl_path, "list", "short", "sinks",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise OutputUnavailableError("aux", "pactl no está instalado") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), PACTL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise OutputUnavailableError("aux", "el servidor de sonido no responde") from exc
        if proc.returncode != 0:
            logger.warning("pactl falló: %s", stderr.decode("utf-8", "replace").strip()[:300])
            raise OutputUnavailableError("aux", "no hay conexión con el servidor de sonido")
        return parse_sinks(stdout.decode("utf-8", "replace"))

    async def _pactl(self, *args: str) -> tuple[int, str]:
        """Ejecuta pactl y devuelve (código, salida). Nunca lanza: -1 si no se pudo ejecutar."""
        try:
            proc = await self._spawn(
                self._settings.music_pactl_path, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            return -1, ""
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), PACTL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, ""
        return proc.returncode or 0, stdout.decode("utf-8", "replace")

    async def set_stream_volume(self, percent: int) -> bool:
        """Cambia el volumen del audio de la música en el servidor de sonido, sin cortarlo.

        Busca el stream por su `application.name` (STREAM_APP). False si no suena nada o pactl falla.
        """
        level = max(0, min(100, int(percent)))
        code, out = await self._pactl("--format=json", "list", "sink-inputs")
        if code != 0:
            return False
        try:
            items = json.loads(out)
        except ValueError:
            return False
        index = next(
            (
                item.get("index")
                for item in items if isinstance(item, dict)
                and (item.get("properties") or {}).get("application.name") == STREAM_APP
            ),
            None,
        )
        if not isinstance(index, int):
            return False
        code, _ = await self._pactl("set-sink-input-volume", str(index), f"{level}%")
        return code == 0

    async def resolve(self, mode: OutputMode) -> str:
        """Nombre del sink para el modo pedido, o OutputUnavailableError."""
        try:
            sinks = await self.list_sinks()
        except OutputUnavailableError as exc:
            raise OutputUnavailableError(mode, exc.reason) from exc
        configured = self._settings.music_bluetooth_sink if mode == "bluetooth" else self._settings.music_aux_sink
        sink = pick_sink(mode, sinks, configured)
        if sink is None:
            reason = "el parlante Bluetooth no está conectado" if mode == "bluetooth" else "no hay salida auxiliar"
            raise OutputUnavailableError(mode, reason)
        return sink
