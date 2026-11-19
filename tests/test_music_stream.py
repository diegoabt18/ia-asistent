import asyncio
import json

import pytest

from app.core.exceptions import UpstreamServiceError
from app.services.music.output import AudioOutputs, OutputUnavailableError, parse_sinks, pick_sink
from app.services.music.stream import PROTOCOL_WHITELIST, STREAM_APP, STREAM_NAME, FfmpegPlayer, redact
from app.services.music.types import AudioSource
from music_fakes import AUX_SINK, BT_SINK, FakeSpawn, music_settings

SOURCE = AudioSource(info_path="/tmp/lumia-music-x/abcdefghijk.json")


def test_downloader_reuses_the_extracted_info_and_writes_audio_to_stdout() -> None:
    args = FfmpegPlayer(music_settings()).downloader_args(SOURCE)
    assert args[0] == "yt-dlp"
    assert args[args.index("--load-info-json") + 1] == SOURCE.info_path
    assert args[args.index("--format") + 1] == "bestaudio/best"
    assert args[args.index("--output") + 1] == "-"
    assert "--ignore-config" in args and "--no-playlist" in args and "--quiet" in args


def test_decoder_reads_only_the_pipe_and_writes_to_the_sink() -> None:
    args = FfmpegPlayer(music_settings()).decoder_args(AUX_SINK)
    assert args[0] == "ffmpeg"
    assert args[args.index("-protocol_whitelist") + 1] == PROTOCOL_WHITELIST == "pipe"
    assert args[args.index("-i") + 1] == "pipe:0"
    assert args[args.index("-threads") + 1] == "1"
    assert args[args.index("-progress") + 1] == "pipe:1"
    assert args[args.index("-f") + 1] == "pulse" and args[args.index("-device") + 1] == AUX_SINK
    assert args[args.index("-name") + 1] == STREAM_APP == "lumia-music"
    assert args[args.index("-af") + 1] == "volume=0.80"  # MUSIC_VOLUME_PERCENT=80 por defecto
    assert args[-1] == STREAM_NAME and STREAM_NAME.isascii()
    assert "-ss" not in args and not any(a.startswith("http") for a in args)


def test_resume_seeks_after_the_pipe_input_and_full_volume_skips_the_filter() -> None:
    args = FfmpegPlayer(music_settings(music_volume_percent=100)).decoder_args(BT_SINK, 42.37)
    assert args.index("-ss") > args.index("-i") and args[args.index("-ss") + 1] == "42.4"
    assert "-af" not in args


def test_missing_binaries_are_reported_clearly() -> None:
    async def missing(*args, **kwargs):
        raise FileNotFoundError(args[0])

    with pytest.raises(UpstreamServiceError) as info:
        asyncio.run(FfmpegPlayer(music_settings(), missing).start(SOURCE, AUX_SINK))
    assert info.value.service == "yt-dlp" and "no está instalado" in info.value.detail

    spawn = FakeSpawn()

    async def only_ytdlp(*args, **kwargs):
        if args[0] == "ffmpeg":
            raise FileNotFoundError("ffmpeg")
        return await spawn(*args, **kwargs)

    with pytest.raises(UpstreamServiceError) as info:
        asyncio.run(FfmpegPlayer(music_settings(), only_ytdlp).start(SOURCE, AUX_SINK))
    assert info.value.service == "FFmpeg" and spawn.downloaders[0].killed  # no deja yt-dlp suelto


def test_playback_tracks_real_progress_and_stop_kills_both_processes() -> None:
    spawn = FakeSpawn(confirm=False)

    async def scenario():
        player = FfmpegPlayer(music_settings(), spawn)
        playback = await player.start(SOURCE, AUX_SINK, 30.0)
        await asyncio.sleep(0)
        before = (playback.started.is_set(), playback.position)
        spawn.decoders[0].progress(2.5)
        await asyncio.sleep(0)
        after = (playback.started.is_set(), playback.position)
        await player.stop(playback)
        return before, after

    before, after = asyncio.run(scenario())
    assert before == (False, 30.0) and after == (True, 32.5)
    assert spawn.running == 0 and spawn.decoders[0].killed and spawn.downloaders[0].killed


def test_error_tail_hides_signed_urls() -> None:
    spawn = FakeSpawn(stderr=b"ERROR: unable to download https://rr1.googlevideo.com/videoplayback?ip=1.2.3.4&sig=x: 403")

    async def scenario():
        player = FfmpegPlayer(music_settings(), spawn)
        playback = await player.start(SOURCE, AUX_SINK)
        tail = await player.error_tail(playback)
        await player.stop(playback)
        return tail

    tail = asyncio.run(scenario())
    assert "googlevideo" not in tail and "1.2.3.4" not in tail and "<url>" in tail
    assert redact("ver https://a.b/c?x=1 ahora") == "ver <url> ahora"


# --- Salidas -----------------------------------------------------------------------------------

HDMI_SINK = "alsa_output.pci-0000_01_00.1.hdmi-stereo"
PACTL = (
    f"0\t{AUX_SINK}\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    f"1\t{HDMI_SINK}\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    f"2\t{BT_SINK}\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
)


def test_parse_sinks_reads_the_name_column() -> None:
    assert parse_sinks(PACTL + "\nbasura\n") == [AUX_SINK, HDMI_SINK, BT_SINK]


def test_pick_sink_auto_and_configured() -> None:
    sinks = [HDMI_SINK, AUX_SINK, BT_SINK]
    assert pick_sink("bluetooth", sinks, "") == BT_SINK
    assert pick_sink("aux", sinks, "") == AUX_SINK  # prefiere la analógica a HDMI
    assert pick_sink("aux", [HDMI_SINK, BT_SINK], "") == HDMI_SINK
    assert pick_sink("bluetooth", [AUX_SINK], "") is None
    assert pick_sink("aux", sinks, HDMI_SINK) == HDMI_SINK
    assert pick_sink("aux", sinks, "no-existe") is None


class PactlProcess:
    def __init__(self, stdout: bytes, code: int = 0) -> None:
        self.stdout = stdout
        self.returncode = code

    async def communicate(self):
        return self.stdout, b"Connection refused"


def pactl(stdout: bytes | dict[str, bytes], code: int = 0):
    """pactl falso. `stdout` puede depender del subcomando (clave = segundo argumento sin opciones)."""
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        if isinstance(stdout, dict):
            key = next((a for a in args[1:] if not a.startswith("-")), "")
            return PactlProcess(stdout.get(key, b""), code)
        return PactlProcess(stdout, code)

    return spawn, calls


def test_outputs_resolve_with_pactl() -> None:
    spawn, calls = pactl(PACTL.encode())
    outputs = AudioOutputs(music_settings(), spawn)
    assert asyncio.run(outputs.resolve("bluetooth")) == BT_SINK
    assert calls[0] == ("pactl", "list", "short", "sinks")


def test_outputs_report_missing_bluetooth_and_dead_sound_server() -> None:
    spawn, _ = pactl(f"0\t{AUX_SINK}\tPipeWire\tx\tIDLE\n".encode())
    with pytest.raises(OutputUnavailableError) as info:
        asyncio.run(AudioOutputs(music_settings(), spawn).resolve("bluetooth"))
    assert info.value.mode == "bluetooth" and "no está conectado" in info.value.reason

    dead, _ = pactl(b"", code=1)
    with pytest.raises(OutputUnavailableError) as info:
        asyncio.run(AudioOutputs(music_settings(), dead).resolve("aux"))
    assert "servidor de sonido" in info.value.reason


SINK_INPUTS = json.dumps(
    [
        {"index": 7, "properties": {"application.name": "Firefox"}},
        {"index": 12, "properties": {"application.name": STREAM_APP, "media.name": STREAM_NAME}},
    ]
).encode()


def test_stream_volume_targets_only_the_music_stream() -> None:
    spawn, calls = pactl({"list": SINK_INPUTS})
    assert asyncio.run(AudioOutputs(music_settings(), spawn).set_stream_volume(35)) is True
    assert calls[0] == ("pactl", "--format=json", "list", "sink-inputs")
    assert calls[1] == ("pactl", "set-sink-input-volume", "12", "35%")


def test_stream_volume_without_music_or_with_broken_pactl_returns_false() -> None:
    nothing, calls = pactl({"list": b"[]"})
    assert asyncio.run(AudioOutputs(music_settings(), nothing).set_stream_volume(50)) is False
    assert len(calls) == 1
    garbage, _ = pactl({"list": b"no es json"})
    assert asyncio.run(AudioOutputs(music_settings(), garbage).set_stream_volume(50)) is False
    dead, _ = pactl(b"", code=1)
    assert asyncio.run(AudioOutputs(music_settings(), dead).set_stream_volume(50)) is False
