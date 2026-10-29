"""Network-denied workflow checks; provider outputs are deliberately synthetic."""

import ipaddress
import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

EXAMPLES = Path(__file__).resolve().parents[2] / "data" / "examples"


@pytest.fixture
def no_external_network(monkeypatch):
    attempts = []
    original_connect = socket.socket.connect
    original_lookup = socket.getaddrinfo

    def local(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            attempts.append(str(address[0]))
            raise OSError("External network disabled by test")
        return original_connect(sock, address)

    def lookup(host, *args, **kwargs):
        if host is not None and not local(host):
            attempts.append(str(host))
            raise OSError("External DNS disabled by test")
        return original_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    yield
    assert attempts == [], f"Unexpected external network attempts: {attempts}"


def test_demo_pipeline_without_external_network(settings, provider, no_external_network):
    # These simple vectors test plumbing, not embedding relevance or model faithfulness.
    async def embed(texts):
        terms = ("grafana", "postgresql", "reverse-proxy")
        return [
            [float(term in text.lower()) for term in terms]
            + [
                float(
                    not any(term in text.lower() for term in terms) and "lunar chess" not in text
                ),
                float("lunar chess" in text),
            ]
            for text in texts
        ]

    provider.embed = embed
    files = ("monitoring.md", "reverse-proxy.yaml", "database.json")
    with TestClient(create_app(settings, provider, provider), base_url="http://localhost") as api:
        for filename in files:
            raw = (EXAMPLES / filename).read_bytes()
            upload = {"file": (filename, raw)}
            document = api.post("/api/documents/upload", files=upload)
            assert document.status_code == 201
            duplicate = api.post("/api/documents/upload", files=upload)
            assert duplicate.json()["id"] == document.json()["id"]
        assert len(api.get("/api/documents").json()) == 3
        updated = api.post(
            "/api/documents/upload",
            files={
                "file": (
                    "database.json",
                    (EXAMPLES / "database.json")
                    .read_bytes()
                    .replace(b'"max_connections": 100', b'"max_connections": 200'),
                )
            },
        )
        # A changed original is a new version, not a silent overwrite.
        assert updated.status_code == 201
        assert len(api.get("/api/documents").json()) == 4
        assert api.delete(f"/api/documents/{updated.json()['id']}").status_code == 204
        stream = api.post("/api/chat", json={"message": "Grafana port?", "stream": True})
        events = [json.loads(line) for line in stream.text.splitlines()]
        sources = events[0]["sources"]
        assert events[-1]["type"] == "done" and events[-1]["grounded"]
        assert events[-1]["quality"] == "validated"
        assert "3000" in "".join(event.get("content", "") for event in events)
        assert sources[0]["filename"] == "monitoring.md" and sources[0]["label"].startswith("S")
        prompt = provider.calls[-1][-1]["content"]
        records = json.loads(
            prompt.split("Untrusted source excerpts:\n", 1)[1].split("\nPrevious user question", 1)[
                0
            ]
        )
        for source in sources:
            lines = (EXAMPLES / source["filename"]).read_text().splitlines()
            assert source["text"] == "\n".join(lines[source["line_start"] - 1 : source["line_end"]])
            record = next(record for record in records if record["source"] == source["label"])
            assert record["excerpt"] == source["text"]
        calls = len(provider.calls)
        missing = api.post("/api/chat", json={"message": "Who won the lunar chess cup?"}).json()
        assert not missing["grounded"] and missing["sources"] == []
        assert "sufficient indexed evidence" in missing["answer"]
        assert len(provider.calls) == calls
        assert api.get("/api/search", params={"q": "3000"}).json()["chunks"]
        log = (EXAMPLES / "proxy-incident.log").read_text()
        body = {"content": log, "service": "reverse-proxy", "use_model": False}
        analysis = api.post("/api/logs/analyse", json=body).json()
        assert analysis["error_count"] == 3
        report = api.post(
            "/api/incidents/summarise",
            json={**body, "notes": (EXAMPLES / "incident-notes.txt").read_text()},
        ).json()
        assert "192.0.2.20:3000" in report["markdown"]
        assert (
            api.post(
                "/api/services",
                json={"name": "Demo Grafana", "kind": "monitoring", "ports": [3000]},
            ).status_code
            == 201
        )
    # Rebuild Qdrant from SQLite, then repeat retrieval and inventory access.
    with TestClient(create_app(settings, provider, provider), base_url="http://localhost") as api:
        assert len(api.get("/api/documents").json()) == 3
        assert api.get("/api/services").json()[0]["name"] == "Demo Grafana"
        assert api.post("/api/chat", json={"message": "Grafana port?"}).json()["sources"]
        document = next(
            doc for doc in api.get("/api/documents").json() if doc["filename"] == "database.json"
        )
        assert api.delete(f"/api/documents/{document['id']}").status_code == 204
        assert api.get("/api/search", params={"q": "5432"}).json()["chunks"] == []
        answer = api.post("/api/chat", json={"message": "PostgreSQL port?"}).json()
        assert not answer["grounded"] and answer["sources"] == []


def test_real_adapters_start_without_network_or_weights(settings, no_external_network):
    with TestClient(create_app(settings), base_url="http://localhost") as api:
        assert api.get("/health").status_code == 200
        assert api.get("/api/status").json()["status"] == "degraded"
        assert (
            api.post("/api/chat", json={"message": "hello", "mode": "general"}).status_code == 503
        )
        assert api.get("/api/search", params={"q": "example"}).status_code == 200
