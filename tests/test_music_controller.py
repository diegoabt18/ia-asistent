import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.services.music.controller import NothingToPlayError
from app.services.music.output import OutputUnavailableError
from music_fakes import AUX_SINK, BT_SINK, FakeExtractor, FakeSpawn, make_controller, settle, track


def test_play_with_query_plays_first_result_and_bumps_generation() -> None:
    controller, extractor, _, _, _ = make_controller()

    async def scenario():
        played = await controller.play(query="soda")
        snap = controller.snapshot()
        await controller.close()
        return played, snap

    played, snap = asyncio.run(scenario())
    assert played == track(1)
    assert extractor.searches == [("soda", 2)]
    assert snap.status == "playing" and snap.current == track(1) and snap.generation == 1


def test_play_without_anything_queued_raises() -> None:
    controller, _, _, _, _ = make_controller()
    with pytest.raises(NothingToPlayError):
        asyncio.run(controller.play())


def test_pause_resume_next_previous() -> None:
    controller, _, _, _, _ = make_controller()

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
        await controller.close()

    asyncio.run(scenario())


# --- Reproducción en el servidor ---------------------------------------------------------------


def test_play_starts_ffmpeg_on_the_aux_output_and_reuses_the_extracted_source() -> None:
    controller, extractor, spawn, _, _ = make_controller()

    async def scenario():
        await controller.play(video_id=track(2).video_id)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert extractor.extractions == [track(2).video_id]  # caché: no repite la extracción
    assert spawn.arg(0, "-i") == f"https://audio.example/{track(2).video_id}"
    assert spawn.arg(0, "-device") == AUX_SINK
    assert snap.sink == AUX_SINK and snap.output_mode == "aux"
    assert spawn.running == 0  # close() mata FFmpeg


def test_pause_stops_ffmpeg_and_resume_continues_where_it_was() -> None:
    controller, _, spawn, _, clock = make_controller()

    async def scenario():
        await controller.play(query="a")
        await settle()
        clock.now += 30
        assert controller.snapshot().position_seconds == 30
        await controller.pause()
        await settle()
        paused = (spawn.running, controller.snapshot().sink, controller.snapshot().position_seconds)
        clock.now += 100  # el tiempo en pausa no cuenta
        await controller.resume()
        await settle()
        await controller.close()
        return paused

    running, sink, position = asyncio.run(scenario())
    assert spawn.processes[0].killed
    assert (running, sink, position) == (0, None, 30)
    assert len(spawn.calls) == 2 and spawn.arg(1, "-ss") == "30.0"


def test_finished_track_advances_and_the_queue_ends_idle() -> None:
    controller, _, spawn, _, _ = make_controller()

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]
        await controller.enqueue(query="b")
        await controller.play()
        await settle()
        spawn.processes[0].finish(0)
        await settle()
        middle = controller.snapshot()
        spawn.processes[1].finish(0)
        await settle()
        end = controller.snapshot()
        await controller.close()
        return middle, end

    middle, end = asyncio.run(scenario())
    assert middle.current == track(2) and middle.status == "playing"
    assert spawn.arg(1, "-i") == f"https://audio.example/{track(2).video_id}" and spawn.arg(1, "-ss") is None
    assert end.status == "idle" and len(spawn.calls) == 2


def test_a_new_track_replaces_the_one_playing() -> None:
    controller, _, spawn, _, _ = make_controller()

    async def scenario():
        await controller.play(query="a")
        await settle()
        controller._extractor.results = [track(2)]
        await controller.play(query="b")
        await settle()
        running = spawn.running
        await controller.close()
        return running

    assert asyncio.run(scenario()) == 1  # nunca dos FFmpeg a la vez
    assert spawn.processes[0].killed
    assert spawn.arg(1, "-i") == f"https://audio.example/{track(2).video_id}"


def test_switching_to_bluetooth_moves_the_song_without_restarting_it() -> None:
    controller, _, spawn, _, clock = make_controller(sinks=[AUX_SINK, BT_SINK])

    async def scenario():
        await controller.play(query="a")
        await settle()
        clock.now += 12
        assert await controller.set_output("bluetooth") == BT_SINK
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert snap.output_mode == "bluetooth" and snap.sink == BT_SINK
    assert spawn.arg(1, "-device") == BT_SINK and spawn.arg(1, "-ss") == "12.0"


def test_unavailable_outputs_are_refused_without_changing_anything() -> None:
    controller, _, spawn, outputs, _ = make_controller()

    async def scenario():
        with pytest.raises(OutputUnavailableError) as info:
            await controller.set_output("bluetooth")
        assert info.value.reason == "el parlante Bluetooth no está conectado"
        outputs.sinks = None  # servidor de sonido caído
        with pytest.raises(OutputUnavailableError):
            await controller.play(query="a")
        return controller.snapshot()

    snap = asyncio.run(scenario())
    assert snap.output_mode == "aux" and snap.status == "idle" and snap.current is None
    assert spawn.calls == []


def test_speaker_turned_off_midway_pauses_with_an_error() -> None:
    controller, _, spawn, outputs, _ = make_controller(sinks=[AUX_SINK, BT_SINK])

    async def scenario():
        await controller.set_output("bluetooth")
        await controller.play(query="a")
        await settle()
        outputs.sinks = [AUX_SINK]  # el parlante se apaga
        spawn.processes[0].finish(1)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert snap.status == "paused" and snap.current == track(1)
    assert snap.error == "sin salida de audio: el parlante Bluetooth no está conectado"
    assert len(spawn.calls) == 1  # no reintenta en bucle


def test_a_track_that_fails_is_skipped() -> None:
    controller, _, spawn, _, _ = make_controller(spawn=FakeSpawn(exit_code=1, stderr=b"403 Forbidden"))

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]
        await controller.enqueue(query="b")
        await controller.play()
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    # Las dos fallan: se saltan, la cola termina y queda el aviso de la última.
    assert len(spawn.calls) == 2 and snap.status == "idle"
    assert snap.error == "no se pudo reproducir «Canción 2»"


def test_upstream_failure_in_search_propagates() -> None:
    controller, _, _, _, _ = make_controller(extractor=FakeExtractor(error=UpstreamServiceError("yt-dlp", "x")))
    with pytest.raises(UpstreamServiceError):
        asyncio.run(controller.play(query="a"))
    assert controller.snapshot().status == "idle"
