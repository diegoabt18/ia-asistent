import asyncio

import pytest

from app.services.music.controller import NothingToPlayError
from app.services.music.player import DeviceReport
from music_fakes import FakeExtractor, FakeSpawn, make_controller, track


async def drain(chunks) -> list[bytes]:
    return [c async for c in chunks]


def test_play_with_query_plays_first_result_and_bumps_generation() -> None:
    controller, extractor, _, _ = make_controller()

    async def scenario():
        played = await controller.play(query="soda")
        return played, controller.snapshot()

    played, snap = asyncio.run(scenario())
    assert played == track(1)
    assert extractor.searches == [("soda", 2)]
    assert snap.status == "playing" and snap.current == track(1) and snap.generation == 1


def test_play_without_anything_queued_raises() -> None:
    controller, _, _, _ = make_controller()
    with pytest.raises(NothingToPlayError):
        asyncio.run(controller.play())


def test_play_with_video_id_extracts_once_and_reuses_source_for_streaming() -> None:
    controller, extractor, spawn, _ = make_controller()

    async def scenario():
        await controller.play(video_id=track(2).video_id)
        session = await controller.open_stream()
        await drain(session.chunks)

    asyncio.run(scenario())
    assert extractor.extractions == [track(2).video_id]  # caché: no repite la extracción
    assert spawn.calls[0][spawn.calls[0].index("-i") + 1] == f"https://audio.example/{track(2).video_id}"


def test_pause_resume_next_previous() -> None:
    controller, _, _, _ = make_controller()

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]  # la segunda búsqueda devuelve otra pista
        await controller.enqueue(query="b")
        assert await controller.play() == track(1)
        assert await controller.pause() is True
        assert await controller.pause() is False
        assert controller.snapshot().status == "paused"
        assert await controller.play() == track(1)  # sin argumentos reanuda
        assert controller.snapshot().status == "playing"
        assert await controller.next() == track(2)
        assert await controller.previous() == track(1)
        await controller.next()
        assert await controller.next() is None
        assert controller.snapshot().status == "playing"  # la última sigue sonando
        await controller.stop()
        assert controller.snapshot().status == "idle"

    asyncio.run(scenario())


def test_stream_returns_none_when_idle() -> None:
    controller, _, spawn, _ = make_controller()
    assert asyncio.run(controller.open_stream()) is None
    assert spawn.calls == []


def test_finished_track_advances_to_the_next_one() -> None:
    controller, _, _, _ = make_controller()

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]
        await controller.enqueue(query="b")
        await controller.play()
        session = await controller.open_stream()
        data = await drain(session.chunks)
        return data, session, controller.snapshot()

    data, session, snap = asyncio.run(scenario())
    assert b"".join(data) == b"aaaabbbbcc"
    assert session.media_type == "audio/mpeg"
    assert snap.current == track(2) and snap.generation == session.generation + 1


def test_pause_stops_sending_audio_until_resume() -> None:
    controller, _, _, _ = make_controller(spawn=FakeSpawn([], endless=True))

    async def scenario():
        await controller.play(query="a")
        session = await controller.open_stream()
        first = await session.chunks.__anext__()
        await controller.pause()
        pending = asyncio.create_task(session.chunks.__anext__())
        for _ in range(20):
            await asyncio.sleep(0)
        blocked = not pending.done()
        await controller.resume()
        second = await asyncio.wait_for(pending, 1)
        await session.chunks.aclose()
        return first, blocked, second

    first, blocked, second = asyncio.run(scenario())
    assert first and second and blocked


def test_track_change_ends_the_open_stream_and_kills_ffmpeg() -> None:
    spawn = FakeSpawn([], endless=True)
    controller, _, _, _ = make_controller(spawn=spawn)

    async def scenario():
        await controller.play(query="a")
        session = await controller.open_stream()
        await session.chunks.__anext__()
        await controller.play(query="b")  # pista nueva: nueva generación
        rest = await asyncio.wait_for(drain(session.chunks), 1)
        return rest

    assert asyncio.run(scenario()) == []
    assert spawn.processes[0].killed and spawn.running == 0


def test_paused_stream_closes_when_stopped() -> None:
    spawn = FakeSpawn([], endless=True)
    controller, _, _, _ = make_controller(spawn=spawn)

    async def scenario():
        await controller.play(query="a")
        session = await controller.open_stream()
        await session.chunks.__anext__()
        await controller.pause()
        pending = asyncio.create_task(drain(session.chunks))
        await asyncio.sleep(0)
        await controller.stop()
        return await asyncio.wait_for(pending, 1)

    assert asyncio.run(scenario()) == []
    assert spawn.running == 0


def test_only_one_stream_is_active_when_the_device_reconnects() -> None:
    spawn = FakeSpawn([], endless=True)
    controller, _, _, _ = make_controller(spawn=spawn)

    async def scenario():
        await controller.play(query="a")
        old = await controller.open_stream()
        await old.chunks.__anext__()
        new = await controller.open_stream()  # el ESP32 reconecta
        old_rest = await asyncio.wait_for(drain(old.chunks), 1)
        await new.chunks.__anext__()
        running = spawn.running
        await new.chunks.aclose()
        return old_rest, running

    old_rest, running_during_new = asyncio.run(scenario())
    assert old_rest == []
    assert running_during_new == 1  # nunca dos FFmpeg a la vez tras la reconexión
    assert spawn.running == 0


def test_stream_error_midway_does_not_advance_the_queue() -> None:
    controller, _, _, _ = make_controller(spawn=FakeSpawn([b"aa"], code=1))

    async def scenario():
        await controller.play(query="a")
        session = await controller.open_stream()
        data = await drain(session.chunks)
        return data, controller.snapshot()

    data, snap = asyncio.run(scenario())
    assert data == [b"aa"]
    assert snap.status == "playing" and snap.current == track(1)


def test_device_disconnect_and_reconnect_are_tracked() -> None:
    controller, _, _, clock = make_controller()
    report = DeviceReport(device_id="esp32-sala", bluetooth_connected=True, bluetooth_sink="JBL")

    assert controller.snapshot().device_connected is False
    controller.heartbeat(report)
    snap = controller.snapshot()
    assert snap.device_connected and snap.device == report and snap.device_reconnects == 0

    clock.now += 16  # más que MUSIC_DEVICE_TIMEOUT_SECONDS (15)
    snap = controller.snapshot()
    assert not snap.device_connected and snap.device_last_seen_seconds == 16

    clock.now += 1
    controller.heartbeat(report)
    snap = controller.snapshot()
    assert snap.device_connected and snap.device_reconnects == 1


def test_upstream_failure_in_search_propagates() -> None:
    from app.core.exceptions import UpstreamServiceError

    controller, _, _, _ = make_controller(extractor=FakeExtractor(error=UpstreamServiceError("yt-dlp", "x")))
    with pytest.raises(UpstreamServiceError):
        asyncio.run(controller.play(query="a"))
    assert controller.snapshot().status == "idle"
