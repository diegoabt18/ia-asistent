import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.dependencies import get_music_controller
from app.main import app
from music_fakes import make_controller, music_settings, track

KEY = "clave-musica"
USER = {"X-API-Key": KEY, "X-User-Ref": "user-music"}
DEVICE = {"Authorization": "Bearer token-esp32", "X-Device-Id": "esp32-sala"}


@pytest.fixture
def setup():
    state = {"settings": music_settings()}
    controller, extractor, spawn, clock = make_controller(state["settings"])
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    app.dependency_overrides[get_music_controller] = lambda: controller
    yield TestClient(app), controller, spawn, state
    app.dependency_overrides.clear()


def test_music_routes_are_hidden_when_disabled(setup) -> None:
    http, _, _, state = setup
    state["settings"] = music_settings(music_enabled=False)
    assert http.get("/api/v1/music/tools", headers=USER).status_code == 404
    assert http.get("/api/v1/music/device/ping", headers=DEVICE).status_code == 404


def test_server_routes_require_api_key_and_user(setup) -> None:
    http, _, _, _ = setup
    assert http.get("/api/v1/music/state").status_code == 401
    assert http.get("/api/v1/music/state", headers={"X-API-Key": KEY}).status_code == 422
    assert http.get("/api/v1/music/state", headers=USER).status_code == 200
    # El token del ESP32 no sirve como API key.
    assert http.get("/api/v1/music/state", headers={**DEVICE, "X-User-Ref": "u"}).status_code == 401


def test_device_routes_require_their_own_token(setup) -> None:
    http, _, _, state = setup
    assert http.get("/api/v1/music/device/ping", headers={"X-Device-Id": "esp"}).status_code == 401
    assert http.get("/api/v1/music/device/ping", headers={**DEVICE, "Authorization": "Bearer otra"}).status_code == 401
    # La API key tampoco abre las rutas del dispositivo.
    assert http.get("/api/v1/music/device/ping", headers={"Authorization": f"Bearer {KEY}", "X-Device-Id": "e"}).status_code == 401
    assert http.get("/api/v1/music/device/ping", headers={**DEVICE, "X-Device-Id": "../x"}).status_code == 422
    response = http.get("/api/v1/music/device/ping", headers=DEVICE)
    assert response.status_code == 200 and response.json()["device_id"] == "esp32-sala"
    state["settings"] = music_settings(music_device_token="")
    assert http.get("/api/v1/music/device/ping", headers=DEVICE).status_code == 503


def test_tools_and_commands_flow(setup) -> None:
    http, controller, _, _ = setup
    tools = http.get("/api/v1/music/tools", headers=USER).json()
    assert {t["name"] for t in tools} >= {"music.play", "music.device_status"}

    response = http.post("/api/v1/music/commands", headers=USER, json={"tool": "music.play", "args": {"query": "soda"}})
    body = response.json()
    assert response.status_code == 200 and body["ok"] is True
    assert body["state"]["status"] == "playing" and body["state"]["current"]["video_id"] == track(1).video_id

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


def test_device_heartbeat_state_and_control(setup) -> None:
    http, controller, _, _ = setup
    beat = http.post(
        "/api/v1/music/device/heartbeat",
        headers=DEVICE,
        json={"bluetooth_connected": True, "bluetooth_sink": "JBL", "wifi_rssi": -55},
    )
    assert beat.status_code == 200
    assert beat.json()["device"] == {
        "connected": True, "device_id": "esp32-sala", "bluetooth_connected": True, "bluetooth_sink": "JBL",
        "wifi_rssi": -55, "buffer_ms": None, "free_heap": None, "firmware": None,
        "last_seen_seconds": 0.0, "reconnects": 0,
    }
    assert http.post("/api/v1/music/device/heartbeat", headers=DEVICE, json={"wifi_rssi": 20}).status_code == 422

    http.post("/api/v1/music/commands", headers=USER, json={"tool": "music.play", "args": {"query": "a"}})
    toggled = http.post("/api/v1/music/device/control", headers=DEVICE, json={"action": "toggle"}).json()
    assert toggled["status"] == "paused"
    assert http.get("/api/v1/music/device/state", headers=DEVICE).json()["status"] == "paused"
    assert http.post("/api/v1/music/device/control", headers=DEVICE, json={"action": "reboot"}).status_code == 422


def test_device_stream_sends_audio_with_format_headers(setup) -> None:
    http, controller, spawn, _ = setup
    assert http.get("/api/v1/music/device/stream", headers=DEVICE).status_code == 204

    http.post("/api/v1/music/commands", headers=USER, json={"tool": "music.play", "args": {"query": "a"}})
    response = http.get("/api/v1/music/device/stream", headers=DEVICE)
    assert response.status_code == 200
    assert response.content == b"aaaabbbbcc"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-music-generation"] == "1"
    assert response.headers["x-track-id"] == track(1).video_id
    assert response.headers["x-audio-format"] == "mp3"
    assert spawn.running == 0
    # Pista terminada y cola vacía: el reproductor vuelve a reposo.
    assert controller.snapshot().status == "idle"


def test_device_stream_upstream_failure_is_502(setup) -> None:
    http, controller, _, _ = setup
    http.post("/api/v1/music/commands", headers=USER, json={"tool": "music.play", "args": {"query": "a"}})
    from app.core.exceptions import UpstreamServiceError

    controller._source_cache = None
    controller._extractor.error = UpstreamServiceError("yt-dlp", "caído")
    response = http.get("/api/v1/music/device/stream", headers=DEVICE)
    assert response.status_code == 502 and response.json() == {"detail": "Error en el servicio yt-dlp"}
