import random

from app.services.music.types import Track


class QueueFullError(Exception):
    pass


class MusicQueue:
    """Cola de reproducción con posición actual. Pura y síncrona: el controlador la protege con un lock.

    `index` apunta a la pista actual (-1 si todavía no se ha reproducido nada). Al pasar de la última
    pista la cola queda "terminada": sin pista actual, pero conservando las escuchadas para volver
    atrás. El tamaño total está acotado por `max_items`.
    """

    def __init__(self, max_items: int) -> None:
        self._max_items = max(1, max_items)
        self._items: list[Track] = []
        self._index = -1
        self._ended = False

    @property
    def items(self) -> list[Track]:
        return list(self._items)

    @property
    def current(self) -> Track | None:
        if self._ended or not 0 <= self._index < len(self._items):
            return None
        return self._items[self._index]

    @property
    def upcoming(self) -> list[Track]:
        return self._items[self._index + 1 :]

    def _make_room(self) -> None:
        if len(self._items) < self._max_items:
            return
        # Primero se descarta la pista más antigua ya escuchada; si no hay, la cola está llena.
        if self._index > 0:
            self._items.pop(0)
            self._index -= 1
            return
        raise QueueFullError("cola llena")

    def add(self, track: Track) -> None:
        """Añade al final, sin cambiar la pista actual."""
        self._make_room()
        self._items.append(track)

    def play_now(self, track: Track) -> Track:
        """Inserta la pista justo después de la actual y la convierte en la actual."""
        self._make_room()
        self._index += 1
        self._items.insert(self._index, track)
        self._ended = False
        return track

    def start(self) -> Track | None:
        """Devuelve la pista actual; si no la hay, avanza a la primera pendiente (si existe)."""
        if self.current is None and self._index + 1 < len(self._items):
            self._index += 1
            self._ended = False
        return self.current

    def next(self) -> Track | None:
        if self._index + 1 >= len(self._items):
            self._ended = bool(self._items)
            return None
        self._index += 1
        self._ended = False
        return self.current

    def previous(self) -> Track | None:
        if self._ended:
            # Tras terminar la cola, "anterior" vuelve a la última pista escuchada.
            self._ended = False
            return self.current
        if self._index <= 0:
            return None
        self._index -= 1
        return self.current

    def skip(self, count: int) -> Track | None:
        """Salta a la pista `count` de las pendientes (1 = la siguiente). None si no existe."""
        target = self._index + count
        if count < 1 or target >= len(self._items):
            return None
        self._index = target
        self._ended = False
        return self.current

    def insert_next(self, track: Track) -> None:
        """Añade la pista justo después de la actual (la siguiente en sonar)."""
        self._make_room()
        self._items.insert(self._index + 1, track)

    def _upcoming_index(self, position: int) -> int | None:
        target = self._index + position
        if position < 1 or target >= len(self._items):
            return None
        return target

    def remove(self, position: int) -> Track | None:
        """Quita la pista `position` de las pendientes (1 = la siguiente)."""
        target = self._upcoming_index(position)
        return None if target is None else self._items.pop(target)

    def move(self, position: int, to: int) -> Track | None:
        """Mueve una pendiente a otra posición de las pendientes (1 = la siguiente)."""
        source = self._upcoming_index(position)
        target = self._upcoming_index(to)
        if source is None or target is None:
            return None
        track = self._items.pop(source)
        self._items.insert(target, track)
        return track

    def clear_upcoming(self) -> int:
        """Vacía las pendientes; conserva la actual y las escuchadas."""
        removed = len(self._items) - (self._index + 1)
        del self._items[self._index + 1 :]
        return max(0, removed)

    def shuffle_upcoming(self, rng: random.Random | None = None) -> None:
        upcoming = self._items[self._index + 1 :]
        (rng or random).shuffle(upcoming)
        self._items[self._index + 1 :] = upcoming

    def has_upcoming(self, video_id: str) -> bool:
        return any(t.video_id == video_id for t in self.upcoming)

    def restart(self) -> Track | None:
        """Vuelve a la primera pista guardada (repetir la cola)."""
        if not self._items:
            return None
        self._index = 0
        self._ended = False
        return self.current

    def clear(self) -> None:
        self._items.clear()
        self._index = -1
        self._ended = False
