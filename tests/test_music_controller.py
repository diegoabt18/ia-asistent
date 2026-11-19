import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.services.music.controller import NothingToPlayError
from app.services.music.output import OutputUnavailableError
from music_fakes import AUX_SINK, BT_SINK, FakeExtractor, FakeSpawn, info_path, make_controller, settle, track


def test_play_with_query_plays_first_result_and_bumps_generation() -> None:
    controller, extractor, _, _, _ = make_controller()

    async def scenario():
        played = await controller.play(query="soda")
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return played, snap

    played, snap = asyncio.run(scenario())
    assert played == track(1)
    assert extractor.searches == [("soda", 2)]
    assert snap.status == "playing" and snap.current == track(1) and snap.generation == 1


def test_status_is_loading_until_ffmpeg_confirms_audio() -> None:
    controller, _, spawn, outputs, _ = make_controller(spawn=FakeSpawn(confirm=False))

    async def scenario():
        await controller.play(query="a")
        await settle()
        loading = controller.snapshot().status
        spawn.decoders[0].progress(0.4)  # primer audio entregado al servidor de sonido
        await settle()
        playing = controller.snapshot()
        await controller.close()
        return loading, playing

    loading, playing = asyncio.run(scenario())
    assert loading == "loading"
    assert playing.status == "playing" and playing.sink == AUX_SINK
    assert outputs.volumes == [70]  # el volumen del reproductor se aplica al confirmar


def test_play_without_anything_queued_raises() -> None:
    controller, _, _, _, _ = make_controller()
    with pytest.raises(NothingToPlayError):
        asyncio.run(controller.play())


def test_pause_resume_next_previous_stop() -> None:
    controller, _, _, _, _ = make_controller()

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]  # la segunda búsqueda devuelve otra pista
        await controller.enqueue(query="b")
        assert await controller.play() == track(1)
        await settle()
        assert await controller.pause() is True
        assert await controller.pause() is False
        assert controller.snapshot().status == "paused"
        assert await controller.play() == track(1)  # sin argumentos reanuda
        await settle()
        assert controller.snapshot().status == "playing"
        assert await controller.next() == track(2)
        assert await controller.previous() == track(1)
        await controller.next()
        assert await controller.next() is None
        await settle()
        assert controller.snapshot().status == "playing"  # la última sigue sonando
        assert await controller.stop() is True
        stopped = controller.snapshot()
        assert stopped.status == "stopped" and stopped.current == track(2)
        assert await controller.stop() is False
        assert await controller.play() == track(2)  # tras detener, vuelve a empezar la pista
        await controller.close()

    asyncio.run(scenario())


# --- Reproducción en el servidor ---------------------------------------------------------------


def test_play_pipes_ytdlp_into_ffmpeg_and_reuses_the_extracted_source() -> None:
    controller, extractor, spawn, _, _ = make_controller()

    async def scenario():
        await controller.play(video_id=track(2).video_id)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert extractor.extractions == [track(2).video_id]  # caché: no repite la extracción
    assert spawn.downloader_arg(0, "--load-info-json") == info_path(track(2).video_id)
    assert spawn.downloader_arg(0, "--output") == "-"
    assert spawn.arg(0, "-i") == "pipe:0"  # FFmpeg nunca abre la URL de YouTube
    assert spawn.arg(0, "-device") == AUX_SINK
    assert snap.sink == AUX_SINK and snap.output_mode == "aux"
    assert spawn.running == 0  # close() mata yt-dlp y FFmpeg


def test_pause_stops_both_processes_and_resume_continues_where_it_was() -> None:
    controller, _, spawn, _, _ = make_controller()

    async def scenario():
        await controller.play(query="a")
        await settle()
        spawn.decoders[0].progress(30)
        await settle()
        assert controller.snapshot().position_seconds == 30
        await controller.pause()
        await settle()
        snap = controller.snapshot()
        paused = (spawn.running, snap.sink, snap.position_seconds, snap.status)
        await controller.resume()
        await settle()
        await controller.close()
        return paused

    running, sink, position, status = asyncio.run(scenario())
    assert spawn.decoders[0].killed and spawn.downloaders[0].killed
    assert (running, sink, position, status) == (0, None, 30, "paused")
    assert len(spawn.decoder_calls) == 2 and spawn.arg(1, "-ss") == "30.0"


def test_seek_restarts_at_the_new_position_and_works_while_paused() -> None:
    controller, _, spawn, _, _ = make_controller()

    async def scenario():
        await controller.play(query="a")
        await settle()
        assert await controller.seek(84) == 84
        await settle()
        playing = controller.snapshot().position_seconds
        await controller.pause()
        await settle()
        assert await controller.seek(500) == 199  # no pasa del final (duración 200)
        paused = controller.snapshot().position_seconds
        await controller.resume()
        await settle()
        await controller.close()
        return playing, paused

    playing, paused = asyncio.run(scenario())
    assert spawn.arg(1, "-ss") == "84.0" and playing == 85  # 84 + 1 s confirmado por el FFmpeg falso
    assert paused == 199 and spawn.arg(2, "-ss") == "199.0"


def test_seek_without_a_track_does_nothing() -> None:
    controller, _, _, _, _ = make_controller()
    assert asyncio.run(controller.seek(10)) is None


def test_volume_applies_live_and_is_clamped() -> None:
    controller, _, _, outputs, _ = make_controller()

    async def scenario():
        assert await controller.set_volume(40) == 40  # sin audio: solo se guarda
        await controller.play(query="a")
        await settle()
        assert await controller.set_volume(150) == 100
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert outputs.volumes == [40, 100] and snap.volume == 100


def test_finished_track_advances_and_the_queue_ends_idle() -> None:
    controller, _, spawn, _, _ = make_controller()

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]
        await controller.enqueue(query="b")
        await controller.play()
        await settle()
        spawn.end_track(0)
        await settle()
        middle = controller.snapshot()
        spawn.end_track(1)
        await settle()
        end = controller.snapshot()
        await controller.close()
        return middle, end

    middle, end = asyncio.run(scenario())
    assert middle.current == track(2) and middle.status == "playing"
    assert spawn.downloader_arg(1, "--load-info-json") == info_path(track(2).video_id)
    assert spawn.arg(1, "-ss") is None
    assert end.status == "idle" and len(spawn.decoder_calls) == 2


def test_jump_plays_a_specific_track_from_the_queue() -> None:
    controller, _, _, _, _ = make_controller()

    async def scenario():
        await controller.play(query="a")
        for n in (2, 3):
            controller._extractor.results = [track(n)]
            await controller.enqueue(query=str(n))
        assert await controller.jump(5) is None
        assert await controller.jump(2) == track(3)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert snap.current == track(3) and snap.upcoming == [] and snap.status == "playing"


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

    assert asyncio.run(scenario()) == 2  # un solo tramo a la vez: un yt-dlp y un FFmpeg
    assert spawn.decoders[0].killed and spawn.downloaders[0].killed
    assert spawn.downloader_arg(1, "--load-info-json") == info_path(track(2).video_id)


def test_switching_to_bluetooth_moves_the_song_without_restarting_it() -> None:
    controller, _, spawn, _, _ = make_controller(sinks=[AUX_SINK, BT_SINK])

    async def scenario():
        await controller.play(query="a")
        await settle()
        spawn.decoders[0].progress(12)
        await settle()
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
        spawn.downloaders[0].finish(0)
        spawn.decoders[0].finish(1)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert snap.status == "paused" and snap.current == track(1)
    assert snap.error == "sin salida de audio: el parlante Bluetooth no está conectado"
    assert len(spawn.decoder_calls) == 1  # no reintenta en bucle


def test_a_cut_download_resumes_from_where_it_was() -> None:
    controller, extractor, spawn, _, _ = make_controller()

    async def scenario():
        await controller.play(query="a")
        await settle()
        spawn.decoders[0].progress(42)
        await settle()
        spawn.downloaders[0].finish(1)  # la conexión se corta
        spawn.decoders[0].finish(0)  # FFmpeg se queda sin datos y termina «bien»
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert snap.current == track(1) and snap.status == "playing"  # no se dio por terminada
    assert spawn.arg(1, "-ss") == "42.0"
    assert extractor.extractions == [track(1).video_id] * 2  # la información se renueva


def test_a_track_that_fails_is_retried_then_skipped() -> None:
    controller, _, spawn, _, _ = make_controller(spawn=FakeSpawn(exit_code=1, stderr=b"ERROR: HTTP Error 403: Forbidden"))

    async def scenario():
        await controller.enqueue(query="a")
        controller._extractor.results = [track(2)]
        await controller.enqueue(query="b")
        await controller.play()
        await settle(200)
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    # Cada pista: 1 intento + 2 reintentos. Las dos fallan: la cola termina con el aviso de la última.
    assert len(spawn.decoder_calls) == 6 and snap.status == "error" and snap.current is None
    assert snap.error == "no se pudo reproducir «Canción 2»"
    assert controller.failure(1) == "no se pudo reproducir «Canción 1»"


def test_wait_until_playing_reports_the_real_outcome() -> None:
    async def outcome(spawn: FakeSpawn):
        controller, _, _, _, _ = make_controller(spawn=spawn)
        segment = controller.segment
        await controller.play(query="a")
        result = await controller.wait_until_playing(controller.last_started_generation, segment, 0.2)
        await controller.close()
        return result

    assert asyncio.run(outcome(FakeSpawn())) == "playing"
    assert asyncio.run(outcome(FakeSpawn(confirm=False))) == "loading"
    assert asyncio.run(outcome(FakeSpawn(exit_code=1))) == "failed"


def test_upstream_failure_in_search_propagates() -> None:
    controller, _, _, _, _ = make_controller(extractor=FakeExtractor(error=UpstreamServiceError("yt-dlp", "x")))
    with pytest.raises(UpstreamServiceError):
        asyncio.run(controller.play(query="a"))
    assert controller.snapshot().status == "idle"
