"""Estado del reproductor. Solo memoria del proceso: una única salida de audio por instancia.

La API corre con un solo proceso de uvicorn (ver Dockerfile); si algún día se usan varios workers,
este estado tendría que pasar a Redis.
"""

import asyncio
from typing import Literal

PlayerStatus = Literal["idle", "playing", "paused"]


class PlayerState:
    """Estado de reproducción. `generation` cambia cada vez que cambia la pista o se detiene: así el
    reproductor distingue "otra pista" de "la misma pista en otra posición o salida"."""

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
        """Despierta a quien espera un cambio (el bucle de reproducción)."""
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

    def change_event(self) -> asyncio.Event:
        """Evento que se activará con el próximo cambio. Se toma ANTES de esperar, sin ceder el control,
        para no perder un cambio que ocurra entre comprobar el estado y empezar a esperar."""
        return self._changed
