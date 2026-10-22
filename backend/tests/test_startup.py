"""Exercise the actual native adapters without weights or injected providers."""

from pathlib import Path

from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_weightless_startup_serves_frontend_and_offline_workflows(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        ai_provider="native",
        embedding_provider="native",
        native_model_path=None,
        native_embedding_model_path=None,
    )
    app = create_app(settings)
    app.mount(
        "/", StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend", html=True)
    )
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/").status_code == 200
        assert client.get("/src/app.js").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/status").json()["status"] == "degraded"
        assert client.get("/api/search", params={"q": "grafana"}).json()["chunks"] == []
        response = client.post(
            "/api/logs/analyse", json={"content": "2026-10-28 ERROR upstream timeout"}
        )
        assert response.status_code == 200
    assert (tmp_path / "grimoire.sqlite3").is_file()
