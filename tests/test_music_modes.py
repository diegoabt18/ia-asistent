"""Cola completa, repetir, aleatorio, reproducción automática, historial y eventos de estado."""

import asyncio

import pytest

from app.services.music.controller import AlreadyQueuedError, title_key
from app.services.music.types import Track
from music_fakes import FakeExtractor, make_controller, music_settings, settle, track


def queued(n: int):
    """Controlador con las pistas 1..n en la cola (la 1 sonando)."""
    extractor = FakeExtractor([track(i) for i in range(1, n + 1)])
    controller, _, spawn, _, _ = make_controller(extractor=extractor)
    return controller, extractor, spawn


async def fill(controller, n: int) -> None:
    for i in range(1, n + 1):
        await controller.enqueue(video_id=track(i).video_id)
    await controller.play()
    await settle()


def ids(tracks: list[Track]) -> list[int]:
    return [int(t.video_id[3:]) for t in tracks]


def test_queue_remove_move_clear_and_play_next() -> None:
    controller, extractor, _ = queued(5)

    async def scenario():
        await fill(controller, 4)
        steps = {"start": ids(controller.snapshot().upcoming)}
        await controller.move(3, 1)  # la 4 pasa a ser la siguiente
        steps["moved"] = ids(controller.snapshot().upcoming)
        assert await controller.remove(2) == track(2)
        steps["removed"] = ids(controller.snapshot().upcoming)
        assert await controller.remove(9) is None and await controller.move(1, 9) is None
        await controller.enqueue(video_id=track(5).video_id, play_next=True)
        steps["next"] = ids(controller.snapshot().upcoming)
        with pytest.raises(AlreadyQueuedError):
            await controller.enqueue(video_id=track(5).video_id)
        steps["cleared"] = await controller.clear_upcoming()
        snap = controller.snapshot()
        await controller.close()
        return steps, snap

    steps, snap = asyncio.run(scenario())
    assert steps["start"] == [2, 3, 4]
    assert steps["moved"] == [4, 2, 3]
    assert steps["removed"] == [4, 3]
    assert steps["next"] == [5, 4, 3]
    assert steps["cleared"] == 3
    assert snap.upcoming == [] and snap.current == track(1) and snap.status == "playing"


def test_repeat_one_restarts_the_same_track() -> None:
    controller, _, spawn = queued(2)

    async def scenario():
        await fill(controller, 2)
        await controller.set_repeat("one")
        before = controller.snapshot().generation
        spawn.end_track(0)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return before, snap

    before, snap = asyncio.run(scenario())
    assert snap.current == track(1) and snap.status == "playing" and snap.repeat_mode == "one"
    assert snap.generation == before + 1 and spawn.arg(1, "-ss") is None  # desde el principio
    assert len(spawn.decoder_calls) == 2


def test_repeat_all_goes_back_to_the_first_track() -> None:
    controller, _, spawn = queued(2)

    async def scenario():
        await fill(controller, 2)
        await controller.set_repeat("all")
        spawn.end_track(0)
        await settle()
        second = controller.snapshot().current
        spawn.end_track(1)
        await settle()
        snap = controller.snapshot()
        await controller.close()
        return second, snap

    second, snap = asyncio.run(scenario())
    assert second == track(2)
    assert snap.current == track(1) and snap.status == "playing" and ids(snap.upcoming) == [2]


def test_next_with_repeat_all_wraps_and_without_it_keeps_playing() -> None:
    controller, _, _ = queued(1)

    async def scenario():
        await fill(controller, 1)
        assert await controller.next() is None  # sin cola ni modos: la actual sigue
        await controller.set_repeat("all")
        wrapped = await controller.next()
        await settle()
        await controller.close()
        return wrapped

    assert asyncio.run(scenario()) == track(1)


def test_shuffle_mixes_only_the_upcoming_tracks() -> None:
    controller, _, _ = queued(8)

    async def scenario():
        await fill(controller, 8)
        await controller.set_shuffle(True)
        snap = controller.snapshot()
        await controller.close()
        return snap

    snap = asyncio.run(scenario())
    assert snap.shuffle is True and snap.current == track(1)
    assert sorted(ids(snap.upcoming)) == [2, 3, 4, 5, 6, 7, 8]


def test_autoplay_adds_a_real_related_track_that_did_not_play_recently() -> None:
    controller, extractor, spawn = queued(2)
    # Recomendaciones: la 2 ya sonó y «Canción 1 (Official Video)» es la misma canción en otra subida.
    reupload = Track(video_id="dupl0000001", title="Canción 1 (Official Video)", artist="Artista 1")
    extractor.related_results = [track(2), reupload, track(7), track(8)]

    async def scenario():
        await fill(controller, 2)
        await controller.set_autoplay(True)
        spawn.end_track(0)  # suena la 2
        await settle()
        spawn.end_track(1)  # fin de la cola: reproducción automática
        await settle(80)
        snap = controller.snapshot()
        history = controller.history()
        await controller.close()
        return snap, history

    snap, history = asyncio.run(scenario())
    assert snap.current is not None and snap.current.video_id == track(7).video_id and snap.status == "playing" and snap.autoplay is True
    assert extractor.related_calls == [track(2).video_id]
    assert ids(history) == [7, 2, 1]


def test_autoplay_skips_unavailable_videos_and_stops_when_none_work() -> None:
    controller, extractor, spawn = queued(1)
    extractor.related_results = [track(5), track(6), track(7), track(8)]
    extractor.unavailable = {track(5).video_id, track(6).video_id}

    async def scenario():
        await fill(controller, 1)
        await controller.set_autoplay(True)
        spawn.end_track(0)
        await settle(80)
        picked = controller.snapshot().current
        extractor.unavailable = {t.video_id for t in extractor.related_results}
        extractor.related_results = [track(9), track(10), track(11), track(12)]
        extractor.unavailable |= {track(i).video_id for i in (9, 10, 11, 12)}
        spawn.end_track(1)
        await settle(80)
        snap = controller.snapshot()
        await controller.close()
        return picked, snap

    picked, snap = asyncio.run(scenario())
    assert picked is not None and picked.video_id == track(7).video_id
    assert snap.status == "idle" and snap.current is None  # ninguna válida: se detiene sin bucles


def test_autoplay_off_or_over_the_limit_stops_at_the_end_of_the_queue() -> None:
    settings = music_settings(music_autoplay_max_tracks=1)
    extractor = FakeExtractor([track(1)])
    extractor.related_results = [track(5), track(6)]
    controller, _, spawn, _, _ = make_controller(settings, extractor=extractor)

    async def scenario():
        await fill(controller, 1)
        spawn.end_track(0)  # reproducción automática desactivada (por defecto)
        await settle()
        off = controller.snapshot().status
        await controller.play(video_id=track(1).video_id)
        await settle()
        await controller.set_autoplay(True)
        spawn.end_track(1)
        await settle(80)
        first = controller.snapshot().current
        spawn.end_track(2)  # ya añadió 1 sin orden del usuario: límite alcanzado
        await settle(80)
        snap = controller.snapshot()
        await controller.close()
        return off, first, snap

    off, first, snap = asyncio.run(scenario())
    assert off == "idle"
    assert first is not None and first.video_id == track(5).video_id
    assert snap.status == "idle"


def test_recommendations_are_cached_and_filtered() -> None:
    controller, extractor, _ = queued(1)
    extractor.related_results = [track(3), track(4, duration=5000)]

    async def scenario():
        first = await controller.recommendations(track(1).video_id)
        second = await controller.recommendations(track(1).video_id)
        await controller.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second == [track(3)]  # la de 83 minutos se descarta
    assert extractor.related_calls == [track(1).video_id]


def test_every_visible_change_raises_the_version_and_wakes_waiters() -> None:
    controller, _, _ = queued(2)

    async def scenario():
        start = controller.version
        waiter = asyncio.ensure_future(controller.wait_change(start, 5))
        await asyncio.sleep(0)
        await controller.enqueue(video_id=track(2).video_id)
        woke = await asyncio.wait_for(waiter, 1)
        unchanged = await controller.wait_change(controller.version, 0.01)
        await controller.set_volume(30)
        snap = controller.snapshot()
        await controller.close()
        return start, woke, unchanged, snap

    start, woke, unchanged, snap = asyncio.run(scenario())
    assert woke > start and unchanged == woke
    assert snap.version > woke and snap.volume == 30


def test_title_key_ignores_decorations_and_the_artist() -> None:
    a = Track(video_id="a" * 11, title="Paramore - Conspiracy (Official Audio)", artist="Paramore")
    b = Track(video_id="b" * 11, title="Conspiracy [Remastered]", artist="Paramore")
    assert title_key(a) == title_key(b) == "conspiracy"
