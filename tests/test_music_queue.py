import pytest

from app.services.music.queue import MusicQueue, QueueFullError
from music_fakes import track


def test_queue_starts_empty_and_starts_with_first_pending() -> None:
    queue = MusicQueue(10)
    assert queue.current is None and queue.start() is None
    queue.add(track(1))
    queue.add(track(2))
    assert queue.current is None
    assert queue.start() == track(1)
    assert queue.upcoming == [track(2)]


def test_next_and_previous_walk_the_queue_and_end_cleanly() -> None:
    queue = MusicQueue(10)
    for n in (1, 2):
        queue.add(track(n))
    queue.start()
    assert queue.next() == track(2)
    assert queue.next() is None and queue.current is None  # fin de la cola
    assert queue.previous() == track(2)  # vuelve a la última escuchada
    assert queue.previous() == track(1)
    assert queue.previous() is None and queue.current == track(1)


def test_adding_after_the_end_does_not_start_playing_until_start() -> None:
    queue = MusicQueue(10)
    queue.add(track(1))
    queue.start()
    queue.next()
    queue.add(track(2))
    assert queue.current is None
    assert queue.start() == track(2)


def test_play_now_inserts_after_current() -> None:
    queue = MusicQueue(10)
    for n in (1, 2):
        queue.add(track(n))
    queue.start()
    assert queue.play_now(track(3)) == track(3)
    assert [t.video_id for t in queue.items] == [track(n).video_id for n in (1, 3, 2)]
    assert queue.next() == track(2)


def test_queue_size_is_bounded_and_drops_played_tracks_first() -> None:
    queue = MusicQueue(2)
    queue.add(track(1))
    queue.add(track(2))
    with pytest.raises(QueueFullError):
        queue.add(track(3))  # nada escuchado que descartar
    queue.start()
    queue.next()  # track(1) ya escuchada
    queue.add(track(3))
    assert len(queue.items) == 2 and queue.current == track(2)
    assert queue.upcoming == [track(3)]


def test_clear_resets_everything() -> None:
    queue = MusicQueue(5)
    queue.add(track(1))
    queue.start()
    queue.clear()
    assert queue.items == [] and queue.current is None and queue.previous() is None
