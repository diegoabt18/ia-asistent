import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.services.music.output import AudioOutputs, OutputUnavailableError, parse_sinks, pick_sink
from app.services.music.stream import PROTOCOL_WHITELIST, STREAM_NAME, FfmpegPlayer
from app.services.music.types import AudioSource
from music_fakes import AUX_SINK, BT_SINK, FakeSpawn, music_settings

SOURCE = AudioSource(url="https://audio.example/x", headers={"User-Agent": "ua"})


def test_player_arguments_restrict_protocols_and_write_to_the_sink() -> None:
    args = FfmpegPlayer(music_settings()).build_args(SOURCE, AUX_SINK)
    assert args[0] == "ffmpeg"
    assert args[args.index("-protocol_whitelist") + 1] == PROTOCOL_WHITELIST == "https,tls,tcp"
    assert args[args.index("-headers") + 1] == "User-Agent: ua\r\n"
    assert args[args.index("-i") + 1] == SOURCE.url
    assert args[args.index("-threads") + 1] == "1"
    assert args[args.index("-f") + 1] == "pulse" and args[args.index("-device") + 1] == AUX_SINK
    assert args[args.index("-af") + 1] == "volume=0.80"  # MUSIC_VOLUME_PERCENT=80 por defecto
    assert args[-1] == STREAM_NAME
    assert "-ss" not in args


def test_resume_seeks_before_the_input_and_full_volume_skips_the_filter() -> None:
    args = FfmpegPlayer(music_settings(music_volume_percent=100)).build_args(SOURCE, BT_SINK, 42.37)
    assert args.index("-ss") < args.index("-i") and args[args.index("-ss") + 1] == "42.4"
    assert "-af" not in args


def test_missing_ffmpeg_binary_is_reported_clearly() -> None:
    async def missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(UpstreamServiceError) as info:
        asyncio.run(FfmpegPlayer(music_settings(), missing).start(SOURCE, AUX_SINK))
    assert "no está instalado" in info.value.detail


def test_stop_kills_a_running_process() -> None:
    spawn = FakeSpawn()

    async def scenario():
        player = FfmpegPlayer(music_settings(), spawn)
        proc = await player.start(SOURCE, AUX_SINK)
        await player.stop(proc)
        return proc

    proc = asyncio.run(scenario())
    assert proc.killed and spawn.running == 0


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


def pactl(stdout: bytes, code: int = 0):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
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
