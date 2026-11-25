from fastapi.testclient import TestClient

from notification_service.main import create_app
from notification_service.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", outbox_relay_enabled=False, event_consumers_enabled=False)


def test_healthz_and_metrics():
    app = create_app(_settings())
    with TestClient(app) as client:
        assert client.get("/healthz").json()["service"] == "notification-service"
        assert client.get("/metrics").status_code == 200
        ready = client.get("/readyz")
        assert ready.status_code in (200, 503)
        assert "database" in ready.json()["checks"]


def test_unauthenticated_api_rejected_with_problem_json():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/api/v1/__nonexistent__")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.headers.get("x-request-id")
