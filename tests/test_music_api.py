import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.dependencies import get_music_controller
from app.main import app
from music_fakes import AUX_SINK, BT_SINK, make_controller, music_settings, track

KEY = "clave-musica"
USER = {"X-API-Key": KEY, "X-User-Ref": "user-music"}


@pytest.fixture
def setup():
    state = {"settings": music_settings()}
    controller, extractor, spawn, outputs, clock = make_controller(state["settings"], sinks=[AUX_SINK, BT_SINK])
    # Cada petición del TestClient usa su propio bucle de eventos: el reproductor en segundo plano se
    # prueba en test_music_controller.py. Aquí solo interesan las rutas y el estado.
    controller._ensure_runner = lambda: None  # type: ignore[method-assign]
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    app.dependency_overrides[get_music_controller] = lambda: controller
    yield TestClient(app), controller, outputs, state
    app.dependency_overrides.clear()


def test_music_routes_are_hidden_when_disabled(setup) -> None:
    http, _, _, state = setup
    state["settings"] = music_settings(music_enabled=False)
    assert http.get("/api/v1/music/tools", headers=USER).status_code == 404
    assert http.get("/api/v1/music/output", headers=USER).status_code == 404


def test_esp32_routes_no_longer_exist(setup) -> None:
    http, _, _, _ = setup
    device = {"Authorization": "Bearer x", "X-Device-Id": "esp32"}
    assert http.get("/api/v1/music/device/ping", headers=device).status_code == 404
    assert http.get("/api/v1/music/device/stream", headers=device).status_code == 404


def test_server_routes_require_api_key_and_user(setup) -> None:
    http, _, _, _ = setup
    assert http.get("/api/v1/music/state").status_code == 401
    assert http.get("/api/v1/music/state", headers={"X-API-Key": KEY}).status_code == 422
    assert http.get("/api/v1/music/state", headers=USER).status_code == 200
    assert http.put("/api/v1/music/output", json={"mode": "aux"}).status_code == 401


def test_tools_and_commands_flow(setup) -> None:
    http, controller, _, _ = setup
    tools = http.get("/api/v1/music/tools", headers=USER).json()
    assert {t["name"] for t in tools} >= {"music.play", "music.output", "music.output_status"}

    response = http.post("/api/v1/music/commands", headers=USER, json={"tool": "music.play", "args": {"query": "soda"}})
    body = response.json()
    assert response.status_code == 200 and body["ok"] is True
    assert body["state"]["status"] == "playing" and body["state"]["current"]["video_id"] == track(1).video_id
    assert body["state"]["output"] == {"mode": "aux", "sink": None}
    assert body["state"]["error"] is None and body["state"]["position_seconds"] == 0

    bad = http.post("/api/v1/music/commands", headers=USER, json={"tool": "music.play", "args": {"url": "x"}})
    assert bad.status_code == 422


def test_search_and_queue_endpoints(setup) -> None:
    http, controller, _, state = setup
    found = http.get("/api/v1/music/search", headers=USER, params={"q": "soda", "limit": 2}).json()
    assert [t["video_id"] for t in found["tracks"]] == [track(1).video_id, track(2).video_id]

    assert http.post("/api/v1/music/queue", headers=USER, json={}).status_code == 422
    assert http.post("/api/v1/music/queue", headers=USER, json={"video_id": "http://x"}).status_code == 422
    added = http.post("/api/v1/music/queue", headers=USER, json={"video_id": track(2).video_id})
    assert added.status_code == 201 and added.json()["title"] == "Canción 2"
    assert controller.snapshot().upcoming == [track(2)]

    state["settings"] = music_settings(music_control_user_refs=["otro"])
    assert http.delete("/api/v1/music/queue", headers=USER).status_code == 403
    state["settings"] = music_settings()
    assert http.delete("/api/v1/music/queue", headers=USER).status_code == 204
    assert controller.snapshot().upcoming == []


def test_output_endpoints(setup) -> None:
    http, controller, outputs, state = setup
    listed = http.get("/api/v1/music/output", headers=USER)
    assert listed.status_code == 200 and listed.json() == {"mode": "aux", "sinks": [AUX_SINK, BT_SINK]}

    changed = http.put("/api/v1/music/output", headers=USER, json={"mode": "bluetooth"})
    assert changed.status_code == 200 and changed.json()["output"]["mode"] == "bluetooth"
    assert http.put("/api/v1/music/output", headers=USER, json={"mode": "hdmi"}).status_code == 422

    outputs.sinks = [AUX_SINK]
    refused = http.put("/api/v1/music/output", headers=USER, json={"mode": "bluetooth"})
    assert refused.status_code == 409 and "no está conectado" in refused.json()["detail"]

    state["settings"] = music_settings(music_control_user_refs=["otro"])
    assert http.put("/api/v1/music/output", headers=USER, json={"mode": "aux"}).status_code == 403

    outputs.sinks = None
    assert http.get("/api/v1/music/output", headers=USER).status_code == 503
    assert controller.snapshot().output_mode == "bluetooth"
