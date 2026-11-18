from app.core.config import Settings
from app.services.music.extract import YtDlpExtractor
from app.services.music.types import Track

QUERY_MAX_CHARS = 200
# Resultados que se piden de más para compensar los que se descartan por duración.
MAX_FETCH = 10


def normalize_query(query: str) -> str:
    text = " ".join(query.split())[:QUERY_MAX_CHARS]
    if not text:
        raise ValueError("búsqueda vacía")
    return text


class MusicSearchService:
    """Busca canciones y aplica las reglas del módulo (límite de resultados y duración máxima)."""

    def __init__(self, settings: Settings, extractor: YtDlpExtractor) -> None:
        self._settings = settings
        self._extractor = extractor

    def clamp_limit(self, limit: int | None) -> int:
        maximum = max(1, self._settings.music_search_limit)
        return maximum if limit is None else min(max(1, limit), maximum)

    def is_playable(self, track: Track) -> bool:
        # Sin duración conocida (p. ej. algunos resultados planos) se acepta; los directos no la tienen
        # pero la extracción posterior los rechaza si no hay audio reproducible.
        return track.duration_seconds is None or track.duration_seconds <= self._settings.music_max_track_seconds

    async def search(self, query: str, limit: int | None = None) -> list[Track]:
        wanted = self.clamp_limit(limit)
        tracks = await self._extractor.search(normalize_query(query), min(MAX_FETCH, wanted * 2))
        return [t for t in tracks if self.is_playable(t)][:wanted]
