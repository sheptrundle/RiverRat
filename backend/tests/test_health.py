import pytest
from fastapi.testclient import TestClient

from riverrat.api.main import app, database_status
from riverrat.config import get_settings
from riverrat.db.session import get_engine


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unreachable_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_health_reports_postgis_version(client):
    app.dependency_overrides[database_status] = lambda: {"ok": True, "postgis": "3.6.0"}

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": {"ok": True, "postgis": "3.6.0"}}


def test_health_stays_up_when_database_is_unreachable(client, unreachable_database):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": {"ok": False, "error": "OperationalError"},
    }
