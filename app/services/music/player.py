"""Estado del reproductor y del ESP32. Solo memoria del proceso: un único dispositivo por instancia.

La API corre con un solo proceso de uvicorn (ver Dockerfile); si algún día se usan varios workers,
este estado tendría que pasar a Redis.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

PlayerStatus = Literal["idle", "playing", "paused"]


@dataclass(frozen=True)
class DeviceReport:
    """Lo que el ESP32 envía en cada latido. Todo opcional salvo el ID del dispositivo."""

    device_id: str
    bluetooth_connected: bool = False
    bluetooth_sink: str | None = None
    wifi_rssi: int | None = None
    buffer_ms: int | None = None
    free_heap: int | None = None
    firmware: str | None = None


class DeviceTracker:
    """Último latido del ESP32. Conectado = latido reciente (menos de `timeout_seconds`)."""

    def __init__(self, timeout_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._timeout = timeout_seconds
        self._clock = clock
        self._last_seen: float | None = None
        self._report: DeviceReport | None = None
        self._reconnects = 0

    def heartbeat(self, report: DeviceReport) -> bool:
        """Registra un latido. Devuelve True si el dispositivo estaba desconectado (reconexión)."""
        was_connected = self.connected
        if not was_connected and self._last_seen is not None:
            self._reconnects += 1
        self._last_seen = self._clock()
        self._report = report
        return not was_connected

    @property
    def connected(self) -> bool:
        return self._last_seen is not None and self._clock() - self._last_seen < self._timeout

    @property
    def report(self) -> DeviceReport | None:
        return self._report

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def seconds_since_seen(self) -> float | None:
        return None if self._last_seen is None else round(self._clock() - self._last_seen, 1)


class PlayerState:
    """Estado de reproducción. `generation` cambia cada vez que cambia la pista o se detiene: el stream
    abierto lo detecta y termina, y el ESP32 sabe que debe reconectar para recibir la pista nueva."""

    def __init__(self) -> None:
        self._status: PlayerStatus = "idle"
        self._generation = 0
        self._changed = asyncio.Event()

    @property
    def status(self) -> PlayerStatus:
        return self._status

    @property
    def generation(self) -> int:
        return self._generation

    def notify(self) -> None:
        """Despierta a quien espera un cambio (p. ej. un stream en pausa que debe cerrarse)."""
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _set(self, status: PlayerStatus) -> None:
        self._status = status
        self.notify()

    def start_track(self) -> None:
        self._generation += 1
        self._set("playing")

    def pause(self) -> bool:
        if self._status != "playing":
            return False
        self._set("paused")
        return True

    def resume(self) -> bool:
        if self._status != "paused":
            return False
        self._set("playing")
        return True

    def stop(self) -> None:
        self._generation += 1
        self._set("idle")

    async def wait_change(self) -> None:
        """Espera al siguiente cambio de estado. El stream lo usa para no enviar audio en pausa."""
        await self._changed.wait()
