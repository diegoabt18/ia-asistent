"""Dobles de prueba del módulo de música: yt-dlp, FFmpeg, pactl y reloj. Sin red, binarios ni audio.

Cada tramo de reproducción lanza dos procesos (ver stream.py): el descargador (yt-dlp) y el
decodificador (FFmpeg). `FakeSpawn` los registra por separado: `decoders` / `downloaders`.
"""

import asyncio

from app.core.config import Settings
from app.core.exceptions import UpstreamServiceError
from app.services.music.controller import MusicController
from app.services.music.output import AudioOutputs, OutputUnavailableError
from app.services.music.stream import FfmpegPlayer
from app.services.music.types import AudioSource, Track

AUX_SINK = "alsa_output.pci-0000_00_1f.3.analog-stereo"
BT_SINK = "bluez_output.AA_BB_CC_DD_EE_FF.1"


def music_settings(**overrides) -> Settings:
    # Espera corta: en las pruebas la confirmación llega en el mismo bucle o no llega.
    values = {"api_key": "clave-musica", "music_enabled": True, "music_start_wait_seconds": 1.0, **overrides}
    return Settings(_env_file=None, **values)


def track(n: int, duration: int | None = 200) -> Track:
    return Track(video_id=f"vid{n:08d}", title=f"Canción {n}", artist=f"Artista {n}", duration_seconds=duration)


def info_path(video_id: str) -> str:
    return f"/tmp/lumia-music-test/{video_id}.json"


class FakeExtractor:
    """Sustituye a YtDlpExtractor: resultados fijos y registro de llamadas."""

    def __init__(self, results: list[Track] | None = None, error: Exception | None = None) -> None:
        self.results = results if results is not None else [track(1), track(2), track(3)]
        self.error = error
        self.searches: list[tuple[str, int]] = []
        self.extractions: list[str] = []
        # Mix de YouTube simulado para las recomendaciones (sin la semilla).
        self.related_results: list[Track] = []
        self.related_calls: list[str] = []
        # IDs que fallan al extraerse (vídeo no disponible).
        self.unavailable: set[str] = set()

    async def related(self, video_id: str, limit: int) -> list[Track]:
        self.related_calls.append(video_id)
        if self.error:
            raise self.error
        return [t for t in self.related_results if t.video_id != video_id][:limit]

    async def search(self, query: str, limit: int) -> list[Track]:
        self.searches.append((query, limit))
        if self.error:
            raise self.error
        return self.results[:limit]

    async def extract(self, video_id: str) -> tuple[Track, AudioSource]:
        self.extractions.append(video_id)
        if self.error:
            raise self.error
        if video_id in self.unavailable:
            raise UpstreamServiceError("yt-dlp", "vídeo no disponible")
        found = next((t for t in self.results if t.video_id == video_id), None)
        found = found or Track(video_id=video_id, title="Directa", artist=None, duration_seconds=180)
        return found, AudioSource(info_path=info_path(video_id))


class FakeStderr:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int) -> bytes:
        return self.data[:size]


class FakeStdout:
    """Salida de `-progress` de FFmpeg: líneas que la prueba empuja con FakeProcess.progress()."""

    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


class FakeProcess:
    """Un proceso que "funciona" hasta que lo matan o la prueba lo termina con finish()."""

    def __init__(self, stderr: bytes = b"", decoder: bool = False) -> None:
        self.stderr = FakeStderr(stderr)
        self.stdout = FakeStdout() if decoder else None
        self.returncode: int | None = None
        self.killed = False
        self._done = asyncio.Event()

    def progress(self, seconds: float) -> None:
        """FFmpeg informa de que ya reprodujo `seconds` segundos."""
        assert self.stdout is not None
        self.stdout.lines.put_nowait(f"out_time_us={int(seconds * 1_000_000)}\n".encode())

    def finish(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self._done.set()
            if self.stdout is not None:
                self.stdout.lines.put_nowait(b"")  # fin de la salida

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


class FakeSpawn:
    """Sustituye a asyncio.create_subprocess_exec.

    - `confirm`: el FFmpeg falso informa enseguida de audio reproducido (1 s), como uno real que suena.
    - `exit_code` != None: ambos procesos terminan solos con ese código (p. ej. 403 → fallo).
    """

    def __init__(self, exit_code: int | None = None, stderr: bytes = b"", confirm: bool = True) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        self.confirm = confirm
        self.calls: list[tuple[str, ...]] = []
        self.processes: list[FakeProcess] = []
        self.decoder_calls: list[tuple[str, ...]] = []
        self.decoders: list[FakeProcess] = []
        self.downloader_calls: list[tuple[str, ...]] = []
        self.downloaders: list[FakeProcess] = []

    async def __call__(self, *args: str, **kwargs) -> FakeProcess:
        decoder = args[0] == "ffmpeg"
        proc = FakeProcess(self.stderr, decoder=decoder)
        self.calls.append(args)
        self.processes.append(proc)
        if decoder:
            self.decoder_calls.append(args)
            self.decoders.append(proc)
            if self.confirm and self.exit_code is None:
                proc.progress(1.0)
        else:
            self.downloader_calls.append(args)
            self.downloaders.append(proc)
        if self.exit_code is not None:
            asyncio.get_running_loop().call_soon(proc.finish, self.exit_code)
        return proc

    def end_track(self, n: int, code: int = 0) -> None:
        """El tramo `n` termina solo: la descarga acaba y FFmpeg sale con `code`."""
        self.downloaders[n].finish(0)
        self.decoders[n].finish(code)

    @property
    def running(self) -> int:
        return sum(1 for p in self.processes if p.returncode is None)

    def arg(self, call: int, flag: str) -> str | None:
        """Valor de una opción en la llamada `call` a FFmpeg (el decodificador)."""
        args = self.decoder_calls[call]
        return args[args.index(flag) + 1] if flag in args else None

    def downloader_arg(self, call: int, flag: str) -> str | None:
        args = self.downloader_calls[call]
        return args[args.index(flag) + 1] if flag in args else None


class FakeOutputs(AudioOutputs):
    """Salidas del host sin pactl. `sinks = None` simula que el servidor de sonido no responde."""

    def __init__(self, settings: Settings, sinks: list[str] | None = None) -> None:
        super().__init__(settings)
        self.sinks: list[str] | None = sinks if sinks is not None else [AUX_SINK]
        self.volumes: list[int] = []

    async def list_sinks(self) -> list[str]:
        if self.sinks is None:
            raise OutputUnavailableError("aux", "no hay conexión con el servidor de sonido")
        return list(self.sinks)

    async def set_stream_volume(self, percent: int) -> bool:
        self.volumes.append(percent)
        return True


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


async def settle(rounds: int = 40) -> None:
    """Deja correr al bucle de reproducción hasta que se estabilice."""
    for _ in range(rounds):
        await asyncio.sleep(0)
