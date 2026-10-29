"""Opt-in real-model deployment rehearsal using disposable storage; no downloads.

Reads ordinary GRIMOIRE_ settings. Reports the actual platform, never certifies Docker.
Python socket guard is not an OS firewall. CPU threads can be bounded in settings;
OS memory limits must be imposed externally (see docs/deployment.md).
"""

import argparse
import ctypes
import json
import os
import platform
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.main import create_app
from app.services.backup import create_backup, restore_backup
from app.services.serving import frontend_directory
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient


def resources():
    rss = None
    if os.name == "nt":

        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak_rss",
                    "rss",
                    "peak_pool",
                    "pool",
                    "peak_nonpool",
                    "nonpool",
                    "page",
                    "peak_page",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb
        ):
            rss = counters.rss
    elif Path("/proc/self/status").exists():
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
    return {
        "rss_bytes": rss,
        "cpu_seconds": time.process_time(),
        "threads": threading.active_count(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".cache/deployment/results.json")
    )
    args = parser.parse_args()
    settings = Settings()
    if settings.ai_provider != "native" or settings.embedding_provider != "native":
        raise RuntimeError(
            "This acceptance requires both pre-provisioned native models."
        )
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "linux": "EXECUTED"
        if platform.system() == "Linux"
        else "BLOCKED — no Linux runtime",
        "docker": "UNEXECUTED — run Compose acceptance separately",
        "network_scope": "Python outbound socket calls denied; loopback allowed; no OS firewall",
        "threads_limit": settings.native_threads,
        "context": settings.native_context_size,
        "samples": [],
        "cycles": [],
        "denied_network": [],
    }
    original_connect, original_resolve = socket.socket.connect, socket.getaddrinfo

    def permitted(host):
        return host in {"localhost", "127.0.0.1", "::1"}

    def connect(sock, address):
        if isinstance(address, tuple) and not permitted(address[0]):
            report["denied_network"].append("connect")
            raise OSError("Outbound networking denied during acceptance")
        return original_connect(sock, address)

    def resolve(host, *positional, **keywords):
        if not permitted(host):
            report["denied_network"].append("resolve")
            raise OSError("External DNS denied during acceptance")
        return original_resolve(host, *positional, **keywords)

    def sample(phase, cycle):
        report["samples"].append({"phase": phase, "cycle": cycle, **resources()})

    with tempfile.TemporaryDirectory(prefix="grimoire-deployment-") as directory:
        root = Path(directory)
        active = settings.model_copy(update={"data_dir": root / "active"})
        question = {
            "message": "What port does Grafana use?",
            "mode": "knowledge",
            "stream": False,
        }
        doc_id = incident_id = first_sources = None
        sample("before_start", 0)
        with (
            patch.object(socket.socket, "connect", connect),
            patch.object(socket, "getaddrinfo", resolve),
        ):
            for cycle in range(3):
                app = create_app(active)
                app.mount("/", StaticFiles(directory=frontend_directory(), html=True))
                started = time.perf_counter()
                with TestClient(app, base_url="http://localhost") as client:
                    assert client.get("/").status_code == 200
                    assert client.get("/src/api.js").status_code == 200
                    assert client.get("/health").status_code == 200
                    deadline = time.monotonic() + 120
                    while (
                        client.get("/api/status").json()["embeddings"]["state"]
                        == "loading"
                    ):
                        if time.monotonic() > deadline:
                            raise RuntimeError("Embedding load deadline exceeded")
                        time.sleep(0.1)
                    assert (
                        client.get("/api/status").json()["embeddings"]["state"]
                        == "ready"
                    )
                    sample("embeddings_loaded_generation_lazy", cycle)
                    if cycle == 0:
                        response = client.post(
                            "/api/documents/ingest",
                            json={
                                "filename": "grafana.md",
                                "service": "grafana",
                                "content": "# Grafana\nGrafana uses port 3000.\n",
                            },
                        )
                        assert response.status_code == 201, response.text
                        doc_id = response.json()["id"]
                        assert (
                            client.post(
                                "/api/services",
                                json={
                                    "name": "grafana",
                                    "kind": "monitoring",
                                    "ports": [3000],
                                },
                            ).status_code
                            == 201
                        )
                        sample("ingestion", cycle)
                    assert client.get("/api/documents").json()[0]["id"] == doc_id
                    assert client.get("/api/services").json()[0]["ports"] == [3000]
                    assert client.get("/api/search", params={"q": "Grafana"}).json()[
                        "chunks"
                    ]
                    cpu_before, wall_before = time.process_time(), time.perf_counter()
                    response = client.post("/api/chat", json=question)
                    assert response.status_code == 200, response.text
                    answer = response.json()
                    assert answer["grounded"] and "3000" in answer["answer"], answer
                    assert all(s["document_id"] == doc_id for s in answer["sources"])
                    if cycle == 0:
                        first_sources = answer["sources"]
                    else:
                        assert answer["sources"] == first_sources
                    sample("generation_and_retrieval", cycle)
                    generation_cpu = time.process_time() - cpu_before
                    generation_wall = time.perf_counter() - wall_before
                    assert client.get("/ready").status_code == 200
                    if cycle == 0:
                        logs = client.post(
                            "/api/logs/analyse", json={"content": "ERROR timeout"}
                        )
                        assert logs.status_code == 200
                        incident = client.post(
                            "/api/incidents/summarise",
                            json={
                                "content": "ERROR timeout",
                                "title": "Disposable deployment incident",
                            },
                        )
                        assert incident.status_code == 200
                        incident_id = incident.json()["id"]
                    assert client.get("/api/incidents").json()[0]["id"] == incident_id
                    idle_wall, idle_cpu = time.perf_counter(), time.process_time()
                    time.sleep(2)
                    sample("idle_loaded", cycle)
                    idle_percent = (
                        100
                        * (time.process_time() - idle_cpu)
                        / (time.perf_counter() - idle_wall)
                    )
                    report["cycles"].append(
                        {
                            "cycle": cycle,
                            "kind": ["initial", "restart", "restore"][cycle],
                            "generation_cpu_seconds": generation_cpu,
                            "generation_wall_seconds": generation_wall,
                            "idle_cpu_percent_one_core": idle_percent,
                            "answer": answer["answer"],
                            "sources": answer["sources"],
                            "seconds": time.perf_counter() - started,
                        }
                    )
                assert app.state.provider.active is None
                assert app.state.embeddings.model is None
                assert app.state.storage.vectors is None
                sample("after_shutdown", cycle)
                if cycle == 1:
                    backup = create_backup(active.data_dir, root / "backups")
                    if active.data_dir.resolve().parent != root.resolve():
                        raise RuntimeError(
                            "Refusing to move state outside disposable acceptance root."
                        )
                    active.data_dir.rename(root / "retired")
                    report["restore_active_state"] = (
                        "Original active directory retired before restore into a new directory"
                    )
                    restore_backup(Path(backup["backup"]), root / "restored")
                    report["backup_manifest"] = backup["manifest"]
                    active = active.model_copy(update={"data_dir": root / "restored"})
            report["disk"] = {
                p.name: p.stat().st_size
                for p in active.data_dir.iterdir()
                if p.is_file()
            }
            report["model_bytes"] = {
                role: path.stat().st_size
                for role, path in (
                    ("generation", settings.native_model_path),
                    ("embedding", settings.native_embedding_model_path),
                )
            }
    assert not report["denied_network"], report["denied_network"]
    report["result"] = "PASS native real-model restart and restore on reported platform"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": report["result"], "report": str(args.output)}))


if __name__ == "__main__":
    main()
