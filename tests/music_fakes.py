"""Dobles de prueba del módulo de música: yt-dlp, FFmpeg, pactl y reloj. Sin red, binarios ni audio."""

import asyncio

from app.core.config import Settings
from app.services.music.controller import MusicController
from app.services.music.output import AudioOutputs, OutputUnavailableError
from app.services.music.stream import FfmpegPlayer
from app.services.music.types import AudioSource, Track

AUX_SINK = "alsa_output.pci-0000_00_1f.3.analog-stereo"
BT_SINK = "bluez_output.AA_BB_CC_DD_EE_FF.1"


def music_settings(**overrides) -> Settings:
    values = {"api_key": "clave-musica", "music_enabled": True, **overrides}
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


class FakeStderr:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int) -> bytes:
        return self.data[:size]


class FakeProcess:
    """Un FFmpeg que "suena" hasta que lo matan o la prueba lo termina con finish()."""

    def __init__(self, stderr: bytes = b"") -> None:
        self.stderr = FakeStderr(stderr)
        self.returncode: int | None = None
        self.killed = False
        self._done = asyncio.Event()

    def finish(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


class FakeSpawn:
    """Sustituye a asyncio.create_subprocess_exec. `exit_code` != None: el proceso termina solo."""

    def __init__(self, exit_code: int | None = None, stderr: bytes = b"") -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls: list[tuple[str, ...]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *args: str, **kwargs) -> FakeProcess:
        self.calls.append(args)
        proc = FakeProcess(self.stderr)
        self.processes.append(proc)
        if self.exit_code is not None:
            asyncio.get_running_loop().call_soon(proc.finish, self.exit_code)
        return proc

    @property
    def running(self) -> int:
        return sum(1 for p in self.processes if p.returncode is None)

    def arg(self, call: int, flag: str) -> str | None:
        args = self.calls[call]
        return args[args.index(flag) + 1] if flag in args else None


class FakeOutputs(AudioOutputs):
    """Salidas del host sin pactl. `sinks = None` simula que el servidor de sonido no responde."""

    def __init__(self, settings: Settings, sinks: list[str] | None = None) -> None:
        super().__init__(settings)
        self.sinks: list[str] | None = sinks if sinks is not None else [AUX_SINK]

    async def list_sinks(self) -> list[str]:
        if self.sinks is None:
            raise OutputUnavailableError("aux", "no hay conexión con el servidor de sonido")
        return list(self.sinks)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_controller(
    settings: Settings | None = None,
    extractor: FakeExtractor | None = None,
    spawn: FakeSpawn | None = None,
    sinks: list[str] | None = None,
):
    settings = settings or music_settings()
    extractor = extractor or FakeExtractor()
    spawn = spawn or FakeSpawn()
    outputs = FakeOutputs(settings, sinks)
    clock = FakeClock()
    controller = MusicController(
        settings,
        extractor=extractor,  # type: ignore[arg-type]
        player=FfmpegPlayer(settings, spawn=spawn),
        outputs=outputs,
        clock=clock,
    )
    return controller, extractor, spawn, outputs, clock


async def settle(rounds: int = 30) -> None:
    """Deja correr al bucle de reproducción hasta que se estabilice."""
    for _ in range(rounds):
        await asyncio.sleep(0)
