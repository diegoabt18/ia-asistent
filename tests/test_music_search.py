import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.services.music.extract import ALLOWED_EXTRACTORS, YtDlpExtractor
from app.services.music.search import MusicSearchService, normalize_query
from app.services.music.types import is_video_id, watch_url
from music_fakes import FakeExtractor, music_settings, track


class FakeYdl:
    def __init__(self, options, answer, calls) -> None:
        self.options = options
        self.answer = answer
        self.calls = calls
        self.closed = False

    def extract_info(self, target, download):
        self.calls.append({"options": self.options, "target": target, "download": download})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def close(self) -> None:
        self.closed = True


def ytdlp(answer):
    calls: list[dict] = []
    created: list[FakeYdl] = []

    def factory(options):
        ydl = FakeYdl(options, answer, calls)
        created.append(ydl)
        return ydl

    return YtDlpExtractor(music_settings(), factory), calls, created


SEARCH_INFO = {
    "entries": [
        {"id": "dQw4w9WgXcQ", "title": " Never Gonna ", "channel": "Rick", "duration": 213},
        {"id": "no-valido", "title": "ID raro"},  # no es un ID de 11 caracteres
        {"id": "abcdefghijk", "title": "", "duration": 100},  # sin título
        {"id": "ABCDEFGHIJK", "title": "Sin duración", "uploader": "Canal"},
    ]
}


def test_video_id_validation_blocks_urls_and_paths() -> None:
    assert is_video_id("dQw4w9WgXcQ")
    for bad in ["http://10.0.0.1/x", "../../etc", "", "a" * 12, None, 123]:
        assert not is_video_id(bad)
    with pytest.raises(ValueError):
        watch_url("http://169.254.169.254/")


def test_search_uses_ytsearch_with_youtube_only_extractors_and_maps_entries() -> None:
    extractor, calls, created = ytdlp(SEARCH_INFO)
    tracks = asyncio.run(extractor.search("rick astley", 5))

    assert calls[0]["target"] == "ytsearch5:rick astley"
    assert calls[0]["download"] is False
    options = calls[0]["options"]
    assert options["allowed_extractors"] == ALLOWED_EXTRACTORS
    assert options["extract_flat"] == "in_playlist" and options["noplaylist"] is True
    assert options["logger"].name == "yt_dlp"  # nada de salida directa a stderr
    assert created[0].closed
    assert [t.video_id for t in tracks] == ["dQw4w9WgXcQ", "ABCDEFGHIJK"]
    assert tracks[0].title == "Never Gonna" and tracks[0].artist == "Rick" and tracks[0].duration_seconds == 213
    assert tracks[1].artist == "Canal" and tracks[1].duration_seconds is None


def test_extract_resolves_https_audio_and_only_forwards_safe_headers() -> None:
    info = {
        "id": "dQw4w9WgXcQ",
        "title": "Tema",
        "duration": 200,
        "url": "https://rr1.googlevideo.com/videoplayback?x=1",
        "http_headers": {"User-Agent": "UA", "X-Evil": "1", "Referer": "a\nInjected: 1"},
    }
    extractor, calls, _ = ytdlp(info)
    found, source = asyncio.run(extractor.extract("dQw4w9WgXcQ"))

    assert calls[0]["target"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert calls[0]["options"]["format"] == "bestaudio/best"
    assert found.title == "Tema"
    assert source.url.startswith("https://rr1.googlevideo.com/")
    assert source.headers == {"User-Agent": "UA"}


def test_extract_rejects_invalid_ids_before_calling_ytdlp() -> None:
    extractor, calls, _ = ytdlp({})
    with pytest.raises(ValueError):
        asyncio.run(extractor.extract("http://127.0.0.1:6379"))
    assert calls == []


def test_extract_rejects_non_https_audio_urls() -> None:
    extractor, _, _ = ytdlp({"id": "dQw4w9WgXcQ", "title": "x", "url": "file:///etc/passwd"})
    with pytest.raises(UpstreamServiceError):
        asyncio.run(extractor.extract("dQw4w9WgXcQ"))


def test_ytdlp_errors_become_upstream_errors_without_leaking_details() -> None:
    class DownloadError(Exception):
        pass

    extractor, _, created = ytdlp(DownloadError("detalle interno con URL"))
    with pytest.raises(UpstreamServiceError) as info:
        asyncio.run(extractor.search("x", 3))
    assert info.value.service == "yt-dlp" and info.value.detail == "DownloadError"
    assert created[0].closed


def test_search_service_normalizes_clamps_and_drops_long_tracks() -> None:
    long_track = track(9, duration=5000)
    extractor = FakeExtractor([track(1), long_track, track(2), track(3)])
    service = MusicSearchService(music_settings(music_search_limit=2), extractor)  # type: ignore[arg-type]

    tracks = asyncio.run(service.search("  soda   stereo ", limit=50))

    assert extractor.searches == [("soda stereo", 4)]
    assert [t.video_id for t in tracks] == [track(1).video_id, track(2).video_id]


def test_search_rejects_empty_queries() -> None:
    with pytest.raises(ValueError):
        normalize_query("   ")
