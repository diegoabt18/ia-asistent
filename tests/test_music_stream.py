import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.services.music.stream import PROTOCOL_WHITELIST, FfmpegTranscoder
from app.services.music.types import AudioSource
from music_fakes import FakeSpawn, music_settings

SOURCE = AudioSource(url="https://audio.example/x", headers={"User-Agent": "ua"})


async def collect(transcoder: FfmpegTranscoder, limit: int | None = None) -> list[bytes]:
    out: list[bytes] = []
    gen = transcoder.stream(SOURCE)
    try:
        async for chunk in gen:
            out.append(chunk)
            if limit is not None and len(out) >= limit:
                break
    finally:
        await gen.aclose()
    return out


def test_mp3_arguments_restrict_protocols_and_use_one_thread() -> None:
    args = FfmpegTranscoder(music_settings(music_bitrate_kbps=96)).build_args(SOURCE)
    assert args[0] == "ffmpeg"
    assert args[args.index("-protocol_whitelist") + 1] == PROTOCOL_WHITELIST == "https,tls,tcp"
    assert args[args.index("-headers") + 1] == "User-Agent: ua\r\n"
    assert args[args.index("-i") + 1] == SOURCE.url
    assert args[args.index("-threads") + 1] == "1"
    assert args[args.index("-b:a") + 1] == "96k" and args[args.index("-f") + 1] == "mp3"
    assert args[-1] == "pipe:1"


def test_pcm_output_is_s16le_with_format_headers() -> None:
    transcoder = FfmpegTranscoder(music_settings(music_output_format="pcm", music_sample_rate=48000))
    args = transcoder.build_args(AudioSource(url="https://a.example/y"))
    assert "-headers" not in args
    assert args[args.index("-f") + 1] == "s16le" and args[args.index("-ar") + 1] == "48000"
    assert transcoder.media_type == "application/octet-stream"
    assert transcoder.audio_headers() == {
        "X-Audio-Format": "pcm_s16le",
        "X-Audio-Sample-Rate": "48000",
        "X-Audio-Channels": "2",
    }


def test_stream_yields_chunks_of_bounded_size_and_reaps_the_process() -> None:
    spawn = FakeSpawn([b"aaaa", b"bbbb", b"cc"])
    chunks = asyncio.run(collect(FfmpegTranscoder(music_settings(music_chunk_bytes=4), spawn)))
    assert chunks == [b"aaaa", b"bbbb", b"cc"]
    proc = spawn.processes[0]
    assert set(proc.stdout.read_sizes) == {4}  # memoria acotada por trozo
    assert proc.returncode == 0 and not proc.killed


def test_closing_the_stream_early_kills_ffmpeg() -> None:
    spawn = FakeSpawn([], endless=True)
    chunks = asyncio.run(collect(FfmpegTranscoder(music_settings(), spawn), limit=3))
    assert len(chunks) == 3
    assert spawn.processes[0].killed and spawn.running == 0


def test_ffmpeg_failure_is_reported_as_upstream_error() -> None:
    spawn = FakeSpawn([b"aa"], code=1, stderr=b"Server returned 403 Forbidden")
    with pytest.raises(UpstreamServiceError) as info:
        asyncio.run(collect(FfmpegTranscoder(music_settings(), spawn)))
    assert info.value.service == "FFmpeg"


def test_missing_ffmpeg_binary_is_reported_clearly() -> None:
    async def missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(UpstreamServiceError) as info:
        asyncio.run(collect(FfmpegTranscoder(music_settings(), missing)))
    assert "no está instalado" in info.value.detail
