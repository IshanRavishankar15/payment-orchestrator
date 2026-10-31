from fastapi.testclient import TestClient

from app.api.routes import health
from app.main import app

client = TestClient(app)


def test_health_reports_service_status() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "payment-orchestrator-api",
    }


def test_readiness_reports_database_dependency(monkeypatch) -> None:
    class ReadyConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return None

    class ReadyEngine:
        def connect(self):
            return ReadyConnection()

    monkeypatch.setattr(health, "engine", ReadyEngine())

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "dependency": "database"}
