"""Disposable state only; these checks do not certify Linux or Docker execution."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.ingestion.parser import Chunk
from app.main import create_app
from app.models import ServiceInput
from app.services.backup import create_backup, restore_backup
from app.services.runtime import embedding_identity
from app.services.storage import Storage


def populated(path):
    store = Storage(path, "test")
    doc = store.add_document(
        {
            "filename": "a.md",
            "source_path": "a.md",
            "document_type": "md",
            "service": "grafana",
            "content_hash": "test",
        },
        [Chunk("Grafana uses port 3000.", 1, 1)],
        [[1, 0]],
        b"Grafana uses port 3000.\n",
    )
    store.save_service(ServiceInput(name="grafana", kind="monitoring", ports=[3000]))
    store.save_incident({"markdown": "# Test incident", "analysis": {}})
    return store, doc


def test_backup_live_wal_restore_all_state(tmp_path):
    store, doc = populated(tmp_path / "active")
    # A held read transaction keeps WAL records live. Snapshot must include them.
    reader = store.connect()
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM documents").fetchall()
    try:
        store.save_service(ServiceInput(name="postgres", kind="database"))
        result = create_backup(tmp_path / "active", tmp_path / "backups")
        restored = tmp_path / "restored"
        restore_backup(Path(result["backup"]), restored)
        recovered = Storage(restored, "test")
        try:
            assert recovered.documents()[0]["id"] == doc["id"]
            assert len(recovered.services()) == 2
            assert recovered.incidents()[0]["markdown"] == "# Test incident"
            assert recovered.semantic_search([1, 0], 3, 0.4)[0].document_id == doc["id"]
            with recovered.connect() as db:
                assert (
                    db.execute("SELECT content FROM originals").fetchone()[0].endswith(b"3000.\n")
                )
        finally:
            recovered.close()
    finally:
        reader.close()
        store.close()


def test_backup_tampering_and_existing_destination_refused(tmp_path):
    store, _ = populated(tmp_path / "active")
    store.close()
    backup = Path(create_backup(tmp_path / "active", tmp_path / "backups")["backup"])
    with pytest.raises(ValueError, match="must not exist"):
        restore_backup(backup, tmp_path / "active")
    with (backup / "grimoire.sqlite3").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_backup_path_and_incomplete_snapshot_safety(tmp_path):
    store, _ = populated(tmp_path / "active")
    try:
        with pytest.raises(ValueError, match="outside"):
            create_backup(tmp_path / "active", tmp_path / "active/backups")
        with patch("app.services.backup.sync_file", side_effect=OSError("disk failure")):
            with pytest.raises(OSError, match="disk failure"):
                create_backup(tmp_path / "active", tmp_path / "backups")
        assert list((tmp_path / "backups").iterdir()) == []
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["future_schema", "mixed_dimensions", "invalid_vectors"])
def test_incompatible_persisted_state_fails_clearly(tmp_path, mode):
    store, _ = populated(tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / "grimoire.sqlite3") as db:
        if mode == "future_schema":
            db.execute("PRAGMA user_version=999")
        elif mode == "mixed_dimensions":
            row = db.execute("SELECT * FROM chunks").fetchone()
            changed = ("7d276a58-302c-405a-ae71-3f417c2fc6fb", *row[1:6], "[1,0,0]", *row[7:])
            db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)", changed)
        else:
            db.execute("UPDATE chunks SET vector='[NaN,0]'")
    with pytest.raises(RuntimeError, match="schema|vectors"):
        Storage(tmp_path, "test")


def test_legacy_schema_upgrades_preserving_documents(tmp_path):
    store, doc = populated(tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / "grimoire.sqlite3") as db:
        db.execute("PRAGMA user_version=0")
        db.execute("DROP TABLE originals")
        db.execute("DROP TABLE incidents")
    store = Storage(tmp_path, "test")
    try:
        assert store.documents()[0]["id"] == doc["id"]
        assert store.incidents() == []
    finally:
        store.close()


def test_embedding_identity_survives_move_and_timestamps(settings, tmp_path):
    first, second = tmp_path / "a.gguf", tmp_path / "b.gguf"
    first.write_bytes(b"GGUF" + b"x" * 1024 * 1024)
    second.write_bytes(first.read_bytes())
    os.utime(second, (1, 1))
    assert embedding_identity(
        settings.model_copy(update={"native_embedding_model_path": first})
    ) == (embedding_identity(settings.model_copy(update={"native_embedding_model_path": second})))


@pytest.mark.parametrize("phase", ["uncommitted", "committed"])
def test_abrupt_process_exit_sqlite_recovery(tmp_path, phase):
    store, _ = populated(tmp_path)
    store.close()
    # os._exit bypasses Python finally blocks and SQLite connection cleanup.
    code = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('BEGIN IMMEDIATE')
db.execute("INSERT INTO services VALUES ('crash','crash','{}')")
if sys.argv[2] == 'committed':
    db.commit()
os._exit(37)
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "grimoire.sqlite3"), phase], timeout=20
    )
    assert process.returncode == 37
    store = Storage(tmp_path, "test")
    try:
        assert len(store.services()) == (2 if phase == "committed" else 1)
        assert len(store.documents()) == 1
        assert store.semantic_search([1, 0], 3, 0.4)
        with store.connect() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()


def test_http_restart_restore_citations_and_shutdown(settings, provider, tmp_path):
    active = settings.model_copy(update={"data_dir": tmp_path / "active"})
    question = {"message": "What port does Grafana use?", "mode": "knowledge", "stream": False}
    with TestClient(create_app(active, provider, provider), base_url="http://localhost") as client:
        assert client.get("/ready").status_code == 200
        assert provider.embedding_calls == 0 and not provider.calls
        doc = client.post(
            "/api/documents/ingest",
            json={"filename": "grafana.md", "content": "# Grafana\nGrafana uses port 3000.\n"},
        ).json()
        client.post("/api/services", json={"name": "grafana", "kind": "monitoring"})
        incident = client.post("/api/incidents/summarise", json={"content": "ERROR timeout"}).json()
        answer = client.post("/api/chat", json=question).json()
        assert answer["grounded"] and "3000" in answer["answer"]
    assert provider.closed
    for restored in (False, True):
        if restored:
            backup = create_backup(active.data_dir, tmp_path / "backups")
            restore_backup(Path(backup["backup"]), tmp_path / "restored")
            active = active.model_copy(update={"data_dir": tmp_path / "restored"})
        # New providers ensure no retained in-process state can satisfy this check.
        generation, embedding = type(provider)(), type(provider)()
        with TestClient(
            create_app(active, generation, embedding), base_url="http://localhost"
        ) as client:
            assert client.get("/api/documents").json()[0]["id"] == doc["id"]
            assert client.get("/api/services").json()[0]["name"] == "grafana"
            assert client.get("/api/incidents").json()[0]["id"] == incident["id"]
            assert client.get("/api/search", params={"q": "3000"}).json()["chunks"]
            repeated = client.post("/api/chat", json=question).json()
            assert repeated["answer"] == answer["answer"]
            assert repeated["sources"] == answer["sources"]
        assert generation.closed and embedding.closed


def test_readiness_failure_does_not_break_liveness(client):
    with patch.object(
        client.app.state.storage, "readiness", side_effect=sqlite3.OperationalError()
    ):
        assert client.get("/ready").status_code == 503
        assert client.get("/health").status_code == 200


def test_backup_cli_roundtrip(tmp_path, monkeypatch, capsys):
    from app.cli import main

    store, _ = populated(tmp_path / "active")
    store.close()
    monkeypatch.setenv("GRIMOIRE_DATA_DIR", str(tmp_path / "active"))
    assert main(["backup", "create", str(tmp_path / "backups"), "--json"]) == 0
    backup = json.loads(capsys.readouterr().out)["backup"]
    assert main(["backup", "restore", backup, str(tmp_path / "restored"), "--json"]) == 0


@pytest.mark.parametrize("phase", ["partial", "complete"])
def test_abrupt_ingestion_rebuilds_only_committed_vectors(tmp_path, phase):
    store, _ = populated(tmp_path)
    store.close()
    code = """
import os, sys
from pathlib import Path
from app.services.storage import Storage
from app.ingestion.parser import Chunk
store = Storage(Path(sys.argv[1]), 'test')
upsert = store.vectors.upsert
def interrupted(*args):
    upsert(*args)
    os._exit(38)
if sys.argv[2] == 'partial':
    store.vectors.upsert = interrupted
store.add_document({'filename':'crash.md','source_path':'crash.md','document_type':'md',
                    'service':None,'content_hash':'crash'},
                   [Chunk('Crash sentinel',1,1)], [[0,1]], b'Crash sentinel')
os._exit(38)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), phase], timeout=20)
    assert result.returncode == 38
    store = Storage(tmp_path, "test")
    try:
        expected = 2 if phase == "complete" else 1
        assert len(store.documents()) == expected
        assert store.vectors.count("chunks").count == expected
        assert len(store.lexical_search("sentinel")) == (expected - 1)
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM originals").fetchone()[0] == expected
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        store.close()


def test_saved_incidents_can_be_pruned(client):
    result = client.post("/api/incidents/summarise", json={"content": "ERROR timeout"})
    assert result.status_code == 200
    incident_id = result.json()["id"]
    assert len(client.get("/api/incidents", params={"limit": 1}).json()) == 1
    assert client.delete(f"/api/incidents/{incident_id}").status_code == 204
    assert client.get("/api/incidents").json() == []
    assert client.delete(f"/api/incidents/{incident_id}").status_code == 404
