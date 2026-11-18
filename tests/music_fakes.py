"""Dobles de prueba del módulo de música: yt-dlp, FFmpeg (subproceso) y reloj. Sin red ni binarios."""

import asyncio

from app.core.config import Settings
from app.services.music.controller import MusicController
from app.services.music.stream import FfmpegTranscoder
from app.services.music.types import AudioSource, Track


def music_settings(**overrides) -> Settings:
    values = {
        "api_key": "clave-musica",
        "music_enabled": True,
        "music_device_token": "token-esp32",
        "music_chunk_bytes": 4,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def track(n: int, duration: int | None = 200) -> Track:
    return Track(video_id=f"vid{n:08d}", title=f"Canción {n}", artist=f"Artista {n}", duration_seconds=duration)


class FakeExtractor:
    """Sustituye a YtDlpExtractor: resultados fijos y registro de llamadas."""

    def __init__(self, results: list[Track] | None = None, error: Exception | None = None) -> None:
        self.results = results if results is not None else [track(1), track(2), track(3)]
        self.error = error
        self.searches: list[tuple[str, int]] = []
        self.extractions: list[str] = []

    async def search(self, query: str, limit: int) -> list[Track]:
        self.searches.append((query, limit))
        if self.error:
            raise self.error
        return self.results[:limit]

    async def extract(self, video_id: str) -> tuple[Track, AudioSource]:
        self.extractions.append(video_id)
        if self.error:
            raise self.error
        found = next((t for t in self.results if t.video_id == video_id), None)
        found = found or Track(video_id=video_id, title="Directa", artist=None, duration_seconds=180)
        return found, AudioSource(url=f"https://audio.example/{video_id}", headers={"User-Agent": "ua"})


class FakeStdout:
    def __init__(self, chunks: list[bytes], endless: bool) -> None:
        self.chunks = list(chunks)
        self.endless = endless
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        await asyncio.sleep(0)
        if self.chunks:
            return self.chunks.pop(0)[:size]
        return b"z" * size if self.endless else b""


class FakeStderr:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int) -> bytes:
        return self.data[:size]


class FakeProcess:
    def __init__(self, chunks: list[bytes], code: int = 0, stderr: bytes = b"", endless: bool = False) -> None:
        self.stdout = FakeStdout(chunks, endless)
        self.stderr = FakeStderr(stderr)
        self.returncode: int | None = None
        self.killed = False
        self._code = code

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._code
        return self.returncode


class FakeSpawn:
    """Sustituye a asyncio.create_subprocess_exec. Cada llamada crea un FakeProcess nuevo."""

    def __init__(self, chunks: list[bytes] | None = None, code: int = 0, stderr: bytes = b"", endless: bool = False) -> None:
        self.chunks = chunks if chunks is not None else [b"aaaa", b"bbbb", b"cc"]
        self.code = code
        self.stderr = stderr
        self.endless = endless
        self.calls: list[tuple[str, ...]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *args: str, **kwargs) -> FakeProcess:
        self.calls.append(args)
        proc = FakeProcess(self.chunks, self.code, self.stderr, self.endless)
        self.processes.append(proc)
        return proc

    @property
    def running(self) -> int:
        return sum(1 for p in self.processes if p.returncode is None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_controller(settings: Settings | None = None, extractor: FakeExtractor | None = None, spawn: FakeSpawn | None = None):
    settings = settings or music_settings()
    extractor = extractor or FakeExtractor()
    spawn = spawn or FakeSpawn()
    clock = FakeClock()
    controller = MusicController(
        settings,
        extractor=extractor,  # type: ignore[arg-type]
        transcoder=FfmpegTranscoder(settings, spawn=spawn),
        clock=clock,
    )
    return controller, extractor, spawn, clock
