import json
import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


class FakeProvider:
    async def start(self):
        pass

    def __init__(self):
        self.calls = []
        self.embedding_calls = 0
        self.closed = False

    async def chat(self, messages):
        self.calls.append(messages)
        if "Classify the supplied log events" in messages[0]["content"]:
            return '{"interpretations":[]}'
        if "Extract supported facts" in messages[0]["content"]:
            slots = json.loads(messages[-1]["content"])["slots"]
            return json.dumps(
                {s["id"]: [s["candidates"][0]["id"]] if s["candidates"] else [] for s in slots}
            )
        if '"insufficient"' in messages[0]["content"]:
            records = json.loads(
                messages[-1]["content"]
                .split("Untrusted source excerpts:\n")[1]
                .split("\nPrevious user question")[0]
            )
            record = next((record for record in records if "3000" in record["excerpt"]), records[0])
            line = next(
                (line for line in record["excerpt"].splitlines() if "3000" in line),
                record["excerpt"].splitlines()[0],
            )
            return json.dumps(
                {"insufficient": False, "claims": [{"source": record["source"], "quote": line}]}
            )
        return "Grafana is exposed on port 3000 [S1]."

    async def stream(self, messages):
        self.calls.append(messages)
        for token in ("Grafana is exposed ", "on port 3000 [S1]."):
            yield token

    async def embed(self, texts):
        self.embedding_calls += 1
        # Deterministic domain embeddings with an orthogonal unrelated direction.
        return [[1.0, 0.0, 0.0] if "grafana" in text.lower() else [0.0, 1.0, 0.0] for text in texts]

    async def health(self):
        return {
            "provider": "fake",
            "model": "test-model",
            "backend": "fake",
            "state": "ready",
            "reason": None,
        }

    async def close(self):
        self.closed = True


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, max_upload_bytes=4096)


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def client(settings, provider):
    with TestClient(
        create_app(settings, provider, provider), base_url="http://localhost"
    ) as client:
        yield client


@pytest.fixture
def indexed(client):
    response = client.post(
        "/api/documents/ingest",
        json={
            "filename": "grafana.md",
            "source_path": "docs/grafana.md",
            "service": "grafana",
            "content": "# Grafana\nGrafana is exposed on port 3000 at 192.0.2.20.\n",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(autouse=True)
def isolate_runtime_configuration(monkeypatch):
    # Unit tests must never inherit provisioned weights or deployment paths from a shell
    # used for explicit model evaluation. Individual tests can still set their own env.
    for key in tuple(os.environ):
        if key.startswith("GRIMOIRE_"):
            monkeypatch.delenv(key)
