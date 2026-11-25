import pytest


def test_refuses_production(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    import fake_providers.main as m

    with pytest.raises(m.FakeProvidersInProduction):
        m.create_app()


def test_refuses_in_kubernetes(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    import fake_providers.main as m

    with pytest.raises(m.FakeProvidersInProduction):
        m.create_app()


def test_runs_in_development(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    from fastapi.testclient import TestClient

    import fake_providers.main as m

    with TestClient(m.create_app()) as c:
        assert c.get("/healthz").status_code == 200
