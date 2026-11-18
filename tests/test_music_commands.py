import asyncio

import pytest

from app.core.exceptions import UpstreamServiceError
from app.prompts.selector_prompt import build_system_prompt
from app.services.music.commands import InvalidCommandError, MusicCommands, tool_specs, validate_args
from app.services.music.player import DeviceReport
from music_fakes import FakeExtractor, make_controller, music_settings, track


def run(commands: MusicCommands, tool: str, args: dict | None = None, user: str = "user-1"):
    return asyncio.run(commands.execute(user, tool, args or {}))


def build(**settings_overrides):
    settings = music_settings(**settings_overrides)
    controller, extractor, spawn, clock = make_controller(settings)
    return MusicCommands(settings, controller), controller, extractor, clock


def test_catalog_has_the_expected_tools_with_permissions() -> None:
    specs = {s.name: s for s in tool_specs()}
    assert set(specs) == {
        "music.play", "music.search", "music.pause", "music.resume",
        "music.next", "music.previous", "music.current", "music.device_status",
    }
    assert specs["music.play"].permission == "music.control"
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
    ],
)
def test_invalid_arguments_are_rejected(tool, args) -> None:
    with pytest.raises(InvalidCommandError):
        validate_args(tool, args)


def test_query_is_normalized_and_truncated() -> None:
    assert validate_args("music.play", {"query": "  a   b " + "x" * 300})["query"].startswith("a b x")
    assert len(validate_args("music.play", {"query": "y" * 500})["query"]) == 200


def test_play_reports_track_and_disconnected_speaker() -> None:
    commands, _, _, _ = build()
    result = run(commands, "music.play", {"query": "soda"})
    assert result.ok and result.tracks == [track(1)]
    assert result.text.startswith("Reproduciendo «Canción 1» de Artista 1.")
    assert "no está conectado" in result.text


def test_search_lists_results_with_duration() -> None:
    commands, _, _, _ = build()
    result = run(commands, "music.search", {"query": "soda"})
    assert result.ok and len(result.tracks) == 3
    assert "1. «Canción 1» de Artista 1 (3:20)" in result.text


def test_controls_and_current_song_texts() -> None:
    commands, controller, _, _ = build()
    assert run(commands, "music.current").text == "No está sonando nada."
    assert run(commands, "music.pause").error == "nothing_playing"
    run(commands, "music.play", {"query": "a"})
    assert run(commands, "music.current").text == "Está sonando: «Canción 1» de Artista 1."
    assert run(commands, "music.pause").ok
    assert run(commands, "music.current").text.startswith("En pausa")
    assert run(commands, "music.resume").text == "Reanudando «Canción 1» de Artista 1."
    assert run(commands, "music.previous").ok is False
    assert run(commands, "music.next").error == "not_found"
    # Sin más canciones, "siguiente" no corta la que suena.
    assert controller.snapshot().status == "playing"


def test_device_status_text() -> None:
    commands, controller, _, clock = build()
    assert run(commands, "music.device_status").text == "El altavoz de música nunca se ha conectado."
    controller.heartbeat(DeviceReport(device_id="esp", bluetooth_connected=True, bluetooth_sink="JBL", wifi_rssi=-60))
    assert run(commands, "music.device_status").text == "El altavoz está conectado (Bluetooth conectado a JBL, Wi-Fi -60 dBm)."
    clock.now += 30
    assert "no está conectado" in run(commands, "music.device_status").text


def test_control_permission_is_enforced_but_read_tools_stay_available() -> None:
    commands, controller, _, _ = build(music_control_user_refs=["user-admin"])
    denied = run(commands, "music.play", {"query": "a"}, user="user-guest")
    assert denied.ok is False and denied.error == "forbidden"
    assert controller.snapshot().status == "idle"
    assert run(commands, "music.current", user="user-guest").ok
    assert run(commands, "music.play", {"query": "a"}, user="user-admin").ok


def test_upstream_failures_become_friendly_answers() -> None:
    settings = music_settings()
    controller, _, _, _ = make_controller(settings, extractor=FakeExtractor(error=UpstreamServiceError("yt-dlp", "x")))
    result = run(MusicCommands(settings, controller), "music.play", {"query": "a"})
    assert result.ok is False and result.error == "upstream"


def test_no_results_is_reported_as_not_found() -> None:
    settings = music_settings()
    controller, _, _, _ = make_controller(settings, extractor=FakeExtractor(results=[]))
    result = run(MusicCommands(settings, controller), "music.play", {"query": "nada"})
    assert result.error == "not_found"


def test_selector_prompt_only_mentions_music_when_its_tools_are_sent() -> None:
    store = [{"name": "products.search", "description": "Buscar productos", "args": []}]
    without = build_system_prompt(store, ["productos"])
    assert "music." not in without
    music = [s.model_dump(include={"name", "description", "args"}) for s in tool_specs()]
    with_music = build_system_prompt(store + music, ["productos", "musica"])
    assert "es music.play" in with_music
    assert '"tool":"music.pause"' in with_music
