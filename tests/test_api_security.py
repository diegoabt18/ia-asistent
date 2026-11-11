from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app


def _override_settings(api_key: str) -> None:
    settings = Settings(api_key=api_key)
    app.dependency_overrides[get_settings] = lambda: settings


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_health_is_public() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_protected_endpoint_refuses_when_no_api_key_configured() -> None:
    _override_settings(api_key="")
    client = TestClient(app)
    response = client.get("/api/v1/models")
    assert response.status_code == 503


def test_protected_endpoint_rejects_wrong_api_key() -> None:
    _override_settings(api_key="correcta")
    client = TestClient(app)
    response = client.get("/api/v1/models", headers={"X-API-Key": "incorrecta"})
    assert response.status_code == 401


def test_protected_endpoint_rejects_missing_api_key() -> None:
    _override_settings(api_key="correcta")
    client = TestClient(app)
    response = client.get("/api/v1/search", params={"q": "test"})
    assert response.status_code == 401
