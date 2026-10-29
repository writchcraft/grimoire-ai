import json

import httpx
import pytest

from app import cli
from app.core.config import Settings
from app.services import diagnostics, runtime
from app.services.storage import Storage


@pytest.fixture(autouse=True)
def environment(monkeypatch, settings):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: None)


def invoke(capsys, *args):
    code = cli.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_help(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("serve", "status", "doctor", "knowledge", "models", "logs", "demo", "validate"):
        assert command in help_text


def test_status_is_read_only(capsys, settings, monkeypatch):
    monkeypatch.setattr(
        runtime, "generation_provider", lambda _: pytest.fail("Must not load models")
    )
    code, data = invoke(capsys, "status", "--json")
    assert code == 0
    assert data["version"].startswith("0.1.0")
    assert data["generation"]["state"] == "not_configured"
    assert data["vector_backend"] == "embedded Qdrant"
    assert data["readiness"] == "unverified/degraded"
    assert not (settings.data_dir / "grimoire.sqlite3").exists()


def test_doctor_healthy_configuration(capsys, settings, monkeypatch):
    path = settings.data_dir / "test.gguf"
    path.write_bytes(b"GGUF" + b"\0" * 20)
    configured = settings.model_copy(
        update={"native_model_path": path, "native_embedding_model_path": path}
    )
    monkeypatch.setattr(cli, "Settings", lambda: configured)
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec", lambda _: object())
    store = Storage(settings.data_dir, runtime.embedding_identity(configured))
    store.close()
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 0
    assert not any(c["status"] == "FAIL" for c in data["checks"])
    assert all(m["state"] == "unverified" for m in data["models"])
    assert not any(m["inference_verified"] for m in data["models"])


def test_missing_gguf(capsys, settings, monkeypatch):
    configured = settings.model_copy(
        update={"native_model_path": settings.data_dir / "missing.gguf"}
    )
    monkeypatch.setattr(cli, "Settings", lambda: configured)
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 1
    assert data["models"][0]["state"] == "model_missing"


def test_docker_and_optional_ollama_unavailable(capsys):
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 1  # Native models are not configured; Docker is not the failure.
    checks = {c["check"]: c for c in data["checks"]}
    assert checks["docker"]["status"] == "SKIP"
    assert checks["docker"]["detail"] == (
        "Docker validation unavailable because the Docker CLI is not installed."
    )
    assert checks["ollama"]["status"] == "SKIP"


def test_configured_ollama_unavailable(capsys, settings, monkeypatch):
    configured = settings.model_copy(update={"ai_provider": "ollama"})
    monkeypatch.setattr(cli, "Settings", lambda: configured)

    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.AsyncClient, "get", offline)
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 1
    generation = next(c for c in data["checks"] if c["check"] == "generation")
    assert generation["status"] == "FAIL"
    assert "Ollama endpoint unavailable" in generation["detail"]


@pytest.mark.parametrize("command", ["doctor", "status", "validate"])
def test_invalid_configuration(capsys, monkeypatch, command):
    monkeypatch.setattr(cli, "Settings", lambda: Settings(ai_provider="secret-invalid-value"))
    code, data = invoke(capsys, command, "--json")
    assert code == 2
    assert data["checks"][0]["status"] in {"FAIL", "BLOCKED"}
    assert "secret-invalid-value" not in json.dumps(data)


def test_doctor_text(capsys):
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "PASS: configuration" in output
    assert "SKIP: docker" in output


def test_safe_ingestion_and_search(capsys, settings, provider, monkeypatch):
    monkeypatch.setattr(runtime, "embedding_provider", lambda _: provider)
    monkeypatch.setattr(runtime, "generation_provider", lambda _: pytest.fail("Generation unused"))
    path = settings.data_dir / "grafana.md"
    path.write_text("# Grafana\nGrafana uses port 3000.\n")
    code, data = invoke(capsys, "knowledge", "ingest", str(path), "--json")
    assert code == 0
    assert data["documents"][0]["source_path"] == str(path)
    assert provider.embedding_calls == 1
    code, data = invoke(capsys, "knowledge", "search", "grafana", "--json")
    assert code == 0
    assert data["chunks"][0]["source_path"] == str(path)
    assert data["chunks"][0]["line_start"] >= 1


def test_oversized_ingestion_fails_before_storage(capsys, settings):
    path = settings.data_dir / "large.md"
    path.write_bytes(b"x" * (settings.max_upload_bytes + 1))
    code, data = invoke(capsys, "knowledge", "ingest", str(path), "--json")
    assert code == 1
    assert "exceeds" in data["error"]
    assert not (settings.data_dir / "grimoire.sqlite3").exists()


def test_model_verification_failure(capsys):
    code, data = invoke(capsys, "models", "verify", "--json")
    assert code == 1
    assert all(c["status"] == "BLOCKED" for c in data["checks"])


def test_model_inspection_header(capsys, settings, monkeypatch):
    path = settings.data_dir / "bad.gguf"
    path.write_bytes(b"not a model")
    monkeypatch.setattr(
        cli, "Settings", lambda: settings.model_copy(update={"native_model_path": path})
    )
    code, data = invoke(capsys, "models", "inspect", "--role", "generation", "--json")
    assert code == 0  # Inspection succeeded; the model is explicitly reported failed.
    model = data["models"][0]
    assert model["state"] == "failed"
    assert model["appears_gguf"] is False
    assert model["size_bytes"] == path.stat().st_size


def test_logs_require_real_generation(capsys, settings):
    path = settings.data_dir / "example.log"
    path.write_text("ERROR connection refused\n")
    code, data = invoke(capsys, "logs", "analyse", str(path), "--json")
    assert code == 1
    assert "GGUF" in data["error"]
    assert "explanation" not in data


def test_validate_records_executed_and_unexecuted_checks(capsys, settings):
    code, data = invoke(capsys, "validate", "--json")
    assert code == 1
    checks = {c["check"]: c for c in data["checks"]}
    assert checks["synthetic_rag_offline"]["status"] == "VERIFIED"
    assert checks["application_import"]["status"] == "VERIFIED"
    assert checks["real_inference_offline"]["status"] == "UNVERIFIED"
    assert checks["docker"]["status"] == "UNVERIFIED"
    assert checks["vector_store"]["status"] == "UNVERIFIED"
    assert not (settings.data_dir / "grimoire.sqlite3").exists()


def test_demo_is_a_truthful_guide(capsys):
    code, data = invoke(capsys, "demo", "--json")
    assert code == 0
    assert "No inference" in data["guide"]
    assert "grimoire serve" in data["steps"]


def test_serve_reuses_web_entry_point(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "serve", lambda: calls.append(True))
    assert cli.main(["serve"]) == 0
    assert calls == [True]


def test_docker_daemon_timeout(monkeypatch):
    import subprocess

    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: "docker")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 5)

    monkeypatch.setattr(diagnostics.subprocess, "run", timeout)
    assert diagnostics.docker_checks()[1][1] == "WARN"


def test_real_serve_assembles_existing_app_and_frontend(monkeypatch, settings):
    from fastapi.testclient import TestClient

    from app.services import serving

    captured = {}
    monkeypatch.setenv("GRIMOIRE_PORT", "7865")
    monkeypatch.setenv("GRIMOIRE_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("GRIMOIRE_AI_PROVIDER", "native")
    monkeypatch.setenv("GRIMOIRE_EMBEDDING_PROVIDER", "native")
    monkeypatch.setenv("GRIMOIRE_NATIVE_MODEL_PATH", "")
    monkeypatch.setenv("GRIMOIRE_NATIVE_EMBEDDING_MODEL_PATH", "")
    monkeypatch.setattr(serving.uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    serving.serve()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7865
    assert captured["access_log"] is False
    with TestClient(captured["app"], base_url="http://localhost") as client:
        assert client.get("/health").status_code == 200
        response = client.get("/")
        assert response.status_code == 200
        assert "Grimoire" in response.text


def test_bad_model_header_is_a_doctor_failure(capsys, settings, monkeypatch):
    path = settings.data_dir / "broken.gguf"
    path.write_bytes(b"invalid")
    monkeypatch.setattr(
        cli, "Settings", lambda: settings.model_copy(update={"native_model_path": path})
    )
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 1
    assert data["models"][0]["appears_gguf"] is False
    assert data["models"][0]["state"] == "failed"


def test_readability_failure_is_reported(capsys, settings, monkeypatch):
    path = settings.data_dir / "unreadable.gguf"
    path.write_bytes(b"GGUF")
    monkeypatch.setattr(
        cli, "Settings", lambda: settings.model_copy(update={"native_model_path": path})
    )
    original = type(path).open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", denied)
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 1
    assert "not a readable" in data["models"][0]["reason"]


def test_invalid_environment_encoding(capsys, monkeypatch):
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setenv("GRIMOIRE_ALLOWED_HOSTS", "[malformed-sensitive-value")
    code, data = invoke(capsys, "doctor", "--json")
    assert code == 2
    assert data["checks"][0]["status"] == "FAIL"
    assert "sensitive" not in json.dumps(data)


def test_storage_failure_returns_operational_exit(capsys, settings, monkeypatch):
    import sqlite3

    (settings.data_dir / "grimoire.sqlite3").touch()

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "Storage", unavailable)
    code, data = invoke(capsys, "knowledge", "search", "grafana", "--json")
    assert code == 1
    assert "database is locked" in data["error"]


def test_hybrid_search_debug_explains_scores_without_loading_generation(
    capsys, settings, provider, monkeypatch
):
    monkeypatch.setattr(runtime, "embedding_provider", lambda _: provider)
    monkeypatch.setattr(runtime, "generation_provider", lambda _: pytest.fail("Generation unused"))
    path = settings.data_dir / "atlas.md"
    path.write_text("# Atlas\nAtlas port: 8123.\n")
    code, _ = invoke(capsys, "knowledge", "ingest", str(path), "--json")
    assert code == 0
    code, data = invoke(capsys, "knowledge", "search", "Atlas port", "--debug", "--json")
    assert code == 0 and data["mode"] == "hybrid"
    selected = data["debug"]["selected_ids"]
    assert selected and selected[0] == data["chunks"][0]["id"]
    assert data["debug"]["components"][selected[0]]["fact_entity"] > 0
    assert data["debug"]["candidate_count"] == 1
