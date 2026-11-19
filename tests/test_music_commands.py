import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.prompts.selector_prompt import build_system_prompt
from app.services.music.commands import (
    InvalidCommandError,
    MusicCommands,
    parse_mode,
    tool_specs,
    validate_args,
)
from music_fakes import AUX_SINK, BT_SINK, FakeExtractor, make_controller, music_settings, settle, track


def build(sinks: list[str] | None = None, **settings_overrides):
    settings = music_settings(**settings_overrides)
    controller, extractor, spawn, outputs, clock = make_controller(settings, sinks=sinks)
    return MusicCommands(settings, controller), controller, outputs


def run_all(commands: MusicCommands, controller, steps):
    """Ejecuta varios comandos en el mismo bucle (el reproductor vive en segundo plano)."""

    async def scenario():
        results = []
        for tool, args, user in steps:
            results.append(await commands.execute(user, tool, args))
            await settle()
        await controller.close()
        return results

    return asyncio.run(scenario())


def step(tool: str, args: dict | None = None, user: str = "user-1"):
    return (tool, args or {}, user)


def test_catalog_has_the_expected_tools_with_permissions() -> None:
    specs = {s.name: s for s in tool_specs()}
    assert set(specs) == {
        "music.play", "music.search", "music.enqueue", "music.queue", "music.jump", "music.pause",
        "music.resume", "music.stop", "music.next", "music.previous", "music.volume", "music.seek",
        "music.current", "music.output", "music.output_status", "music.play_next", "music.remove",
        "music.move", "music.clear_queue", "music.shuffle", "music.repeat", "music.autoplay",
        "music.recommend", "music.history",
    }
    assert specs["music.recommend"].permission == specs["music.history"].permission == "music.read"
    assert specs["music.repeat"].args[0].kind == ["off", "one", "all"]
    assert specs["music.stop"].permission == specs["music.volume"].permission == "music.control"
    assert specs["music.queue"].permission == "music.read"
    assert [a.kind for a in specs["music.volume"].args] == ["int", ["up", "down"]]
    assert specs["music.play"].permission == "music.control"
    assert specs["music.output"].permission == "music.control"
    assert specs["music.output"].args[0].kind == ["aux", "bluetooth"]
    assert specs["music.current"].permission == "music.read"
    assert specs["music.search"].args[0].required is True
    assert all(s.module == "musica" for s in specs.values())


@pytest.mark.parametrize(
    "tool,args",
    [
        ("music.unknown", {}),
        ("music.pause", {"query": "x"}),
        ("music.play", {"url": "http://10.0.0.1"}),
        ("music.play", {"query": 5}),
        ("music.search", {}),
        ("music.search", {"query": "   "}),
        ("music.output", {}),
        ("music.volume", {"level": 101}),
        ("music.volume", {"level": True}),
        ("music.volume", {"change": "max"}),
        ("music.seek", {"position": "-5"}),
        ("music.seek", {}),
        ("music.jump", {"position": "dos"}),
    ],
)
def test_invalid_arguments_are_rejected(tool, args) -> None:
    with pytest.raises(InvalidCommandError):
        validate_args(tool, args)


def test_integer_arguments_accept_numbers_and_numeric_text() -> None:
    assert validate_args("music.volume", {"level": "35"}) == {"level": 35}
    assert validate_args("music.volume", {"change": "up"}) == {"change": "up"}
    assert validate_args("music.seek", {"position": 84}) == {"position": 84}


def test_query_is_normalized_and_truncated() -> None:
    assert validate_args("music.play", {"query": "  a   b " + "x" * 300})["query"].startswith("a b x")
    assert len(validate_args("music.play", {"query": "y" * 500})["query"]) == 200


def test_output_mode_aliases() -> None:
    assert parse_mode("Bluetooth") == parse_mode("parlante bluetooth") == parse_mode("bt") == "bluetooth"
    assert parse_mode("aux") == parse_mode("Cable") == parse_mode("jack") == "aux"
    assert parse_mode("hdmi") is None


def test_play_and_search_texts() -> None:
    commands, controller, _ = build()
    play, search = run_all(commands, controller, [step("music.play", {"query": "soda"}), step("music.search", {"query": "soda"})])
    assert play.ok and play.tracks == [track(1)] and play.text == "Reproduciendo «Canción 1» de Artista 1."
    assert search.ok and len(search.tracks) == 3
    assert "1. «Canción 1» de Artista 1 (3:20)" in search.text


def test_controls_and_current_song_texts() -> None:
    commands, controller, _ = build()
    results = run_all(commands, controller, [
        step("music.current"),
        step("music.pause"),
        step("music.play", {"query": "a"}),
        step("music.current"),
        step("music.pause"),
        step("music.current"),
        step("music.resume"),
        step("music.previous"),
        step("music.next"),
        step("music.current"),
    ])
    assert results[0].text == "No está sonando nada."
    assert results[1].error == "nothing_playing"
    assert results[3].text == "Está sonando: «Canción 1» de Artista 1 (0:01 de 3:20)."
    assert results[4].ok and results[5].text.startswith("En pausa")
    assert results[6].text == "Reanudando «Canción 1» de Artista 1."
    assert results[7].ok is False and results[8].error == "not_found"
    # Sin más canciones, "siguiente" no corta la que suena.
    assert results[9].text.startswith("Está sonando")


def test_stop_volume_seek_and_queue_commands() -> None:
    commands, controller, outputs = build()
    results = run_all(commands, controller, [
        step("music.stop"),
        step("music.play", {"query": "a"}),
        step("music.enqueue", {"query": "b"}),
        step("music.queue"),
        step("music.volume", {"level": 35}),
        step("music.volume", {"change": "up"}),
        step("music.seek", {"position": 84}),
        step("music.jump", {"position": 3}),
        step("music.jump", {"position": 1}),
        step("music.stop"),
        step("music.current"),
    ])
    assert results[0].error == "nothing_playing"
    assert results[2].text == "Añadí «Canción 1» de Artista 1 a la cola (posición 1)."
    assert results[3].text == "Ahora: «Canción 1» de Artista 1.\nEn la cola:\n1. «Canción 1» de Artista 1 (3:20)"
    assert results[4].text == "Volumen al 35 %." and results[5].text == "Volumen al 45 %."
    assert results[6].text == "Listo, voy al 1:24."
    assert results[7].error == "not_found"
    assert results[8].ok and results[8].text == "Reproduciendo «Canción 1» de Artista 1."
    assert results[9].text == "Música detenida." and results[10].text == "No está sonando nada."


def test_play_never_claims_sound_that_did_not_start() -> None:
    from music_fakes import FakeSpawn

    settings = music_settings(music_start_wait_seconds=0.2)
    controller, _, _, _, _ = make_controller(settings, spawn=FakeSpawn(confirm=False))
    loading = run_all(MusicCommands(settings, controller), controller, [step("music.play", {"query": "a"})])[0]
    assert loading.ok and loading.text.startswith("Cargando «Canción 1»")

    controller, _, _, _, _ = make_controller(settings, spawn=FakeSpawn(exit_code=1))
    failed = run_all(MusicCommands(settings, controller), controller, [step("music.play", {"query": "a"})])[0]
    assert not failed.ok and failed.error == "playback_failed"
    assert failed.text == "No pude reproducir «Canción 1» de Artista 1: no se pudo reproducir «Canción 1»."


def test_output_commands() -> None:
    commands, controller, outputs = build(sinks=[AUX_SINK])
    results = run_all(commands, controller, [
        step("music.output", {"mode": "bluetooth"}),
        step("music.output_status"),
        step("music.output", {"mode": "hdmi"}),
    ])
    assert results[0].ok is False and results[0].error == "output_unavailable"
    assert results[0].text == "No puedo sonar por el parlante Bluetooth: el parlante Bluetooth no está conectado."
    assert results[1].text == (
        "No suena nada ahora; la salida elegida es el cable auxiliar. "
        "Bluetooth: el parlante Bluetooth no está conectado."
    )
    assert results[2].error == "invalid_output"

    commands, controller, _ = build(sinks=[AUX_SINK, BT_SINK])
    results = run_all(commands, controller, [
        step("music.output", {"mode": "bluetooth"}),
        step("music.play", {"query": "a"}),
        step("music.output_status"),
    ])
    assert results[0].ok and results[0].text == "Listo, la música sonará por el parlante Bluetooth."
    assert results[2].text == "La música está sonando por el parlante Bluetooth. El parlante Bluetooth está conectado."


def test_play_without_sound_output_is_a_friendly_error() -> None:
    commands, controller, outputs = build()
    outputs.sinks = None
    (result,) = run_all(commands, controller, [step("music.play", {"query": "a"})])
    assert result.ok is False and result.error == "output_unavailable"
    assert "servidor de sonido" in result.text


def test_control_permission_is_enforced_but_read_tools_stay_available() -> None:
    commands, controller, _ = build(music_control_user_refs=["user-admin"])
    denied, output, current, allowed = run_all(commands, controller, [
        step("music.play", {"query": "a"}, user="user-guest"),
        step("music.output", {"mode": "aux"}, user="user-guest"),
        step("music.current", user="user-guest"),
        step("music.play", {"query": "a"}, user="user-admin"),
    ])
    assert denied.error == "forbidden" and output.error == "forbidden"
    assert current.ok and allowed.ok


def test_upstream_failures_become_friendly_answers() -> None:
    settings = music_settings()
    controller, *_ = make_controller(settings, extractor=FakeExtractor(error=UpstreamServiceError("yt-dlp", "x")))
    result = asyncio.run(MusicCommands(settings, controller).execute("u", "music.play", {"query": "a"}))
    assert result.ok is False and result.error == "upstream"


def test_no_results_is_reported_as_not_found() -> None:
    settings = music_settings()
    controller, *_ = make_controller(settings, extractor=FakeExtractor(results=[]))
    result = asyncio.run(MusicCommands(settings, controller).execute("u", "music.play", {"query": "nada"}))
    assert result.error == "not_found"


def test_selector_prompt_only_mentions_music_when_its_tools_are_sent() -> None:
    store = [{"name": "products.search", "description": "Buscar productos", "args": []}]
    without = build_system_prompt(store, ["productos"])
    assert "music." not in without
    music = [s.model_dump(include={"name", "description", "args"}) for s in tool_specs()]
    with_music = build_system_prompt(store + music, ["productos", "musica"])
    assert "es music.play" in with_music and "es music.output" in with_music
    assert '"tool":"music.pause"' in with_music
    assert '"mode":"bluetooth"' in with_music
    assert "mode:aux|bluetooth" in with_music


def test_new_queue_and_mode_commands_answer_with_real_outcomes() -> None:
    commands, controller, _ = build()
    vid = track(2).video_id
    results = run_all(commands, controller, [
        step("music.play", {"video_id": track(1).video_id}),
        step("music.enqueue", {"video_id": vid}),
        step("music.enqueue", {"video_id": vid}),
        step("music.play_next", {"video_id": track(3).video_id}),
        step("music.move", {"position": 2, "to": 1}),
        step("music.remove", {"position": 1}),
        step("music.shuffle", {"state": "on"}),
        step("music.repeat", {"repeat": "all"}),
        step("music.autoplay", {"state": "on"}),
        step("music.clear_queue"),
        step("music.remove", {"position": 1}),
        step("music.history"),
    ])
    texts = [r.text for r in results]
    assert results[0].ok and texts[0].startswith("Reproduciendo «Canción 1»")
    assert texts[1] == "Añadí «Canción 2» de Artista 2 a la cola (posición 1)."
    assert results[2].error == "duplicate" and texts[2] == "«Canción 2» de Artista 2 ya está en la cola."
    assert texts[3] == "«Canción 3» de Artista 3 sonará a continuación."
    assert texts[4] == "«Canción 2» de Artista 2 ahora es la número 1 de la cola."
    assert texts[5] == "Quité «Canción 2» de Artista 2 de la cola."
    assert texts[6:9] == ["Modo aleatorio activado.", "Repetiré toda la cola.", texts[8]]
    assert texts[8].startswith("Reproducción automática activada")
    assert texts[9] == "Vacié la cola (1 canciones)."
    assert results[10].error == "not_found"
    assert texts[11].startswith("Sonaron hace poco:\n1. «Canción 1»")


@pytest.mark.parametrize(
    "tool,args",
    [
        ("music.enqueue", {}),
        ("music.enqueue", {"query": "x", "video_id": "abcdefghijk"}),
        ("music.play", {"video_id": "https://x.y/z"}),
        ("music.repeat", {"repeat": "siempre"}),
        ("music.shuffle", {"state": "maybe"}),
        ("music.move", {"position": 1, "to": 0}),
    ],
)
def test_new_commands_reject_bad_arguments(tool, args) -> None:
    with pytest.raises(InvalidCommandError):
        validate_args(tool, args)


def test_recommend_lists_real_related_tracks() -> None:
    commands, controller, _ = build()
    controller._extractor.related_results = [track(4), track(5)]
    results = run_all(commands, controller, [step("music.recommend"), step("music.play", {"query": "a"}), step("music.recommend")])
    assert results[0].error == "nothing_playing"
    assert results[2].ok and results[2].text.startswith("Parecidas a «Canción 1»")
    assert [t.video_id for t in results[2].tracks] == [track(4).video_id, track(5).video_id]
