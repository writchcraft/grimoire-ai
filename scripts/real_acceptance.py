"""Explicit opt-in real CPU acceptance; never collected by pytest, never downloads weights.

Run from repository root after sourcing .env.native.ps1. Uses a fresh temporary index.
Socket guard denies Python network calls except loopback; not an OS-level firewall.
"""

import ctypes
import ipaddress
import json
import os
import socket
import tempfile
import time
from pathlib import Path

from app.ai.native import NativeEmbeddings, NativeProvider
from app.core.config import Settings
from app.main import create_app
from app.services.runtime import wait_ready
from fastapi.testclient import TestClient
from llama_cpp import Llama

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".cache/real-model-acceptance/workflows.json"
report = {"loads": [], "requests": [], "external_attempts": []}


def memory():
    if os.name != "nt":
        return None

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
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb
    )
    return counters.rss


def loader(**options):
    start, before = time.perf_counter(), memory()
    model = Llama(**options)
    report["loads"].append(
        {
            "embedding": options["embedding"],
            "seconds": time.perf_counter() - start,
            "rss_before": before,
            "rss_after": memory(),
            "architecture": model.metadata.get("general.architecture"),
            "pooling": model.metadata.get("bert.pooling_type"),
        }
    )
    return model


def guard(host):
    if host is None or host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    report["external_attempts"].append(str(host))
    raise OSError("External networking denied during real acceptance")


original_connect, original_lookup = socket.socket.connect, socket.getaddrinfo


def connect(sock, address):
    if isinstance(address, tuple):
        guard(address[0])
    return original_connect(sock, address)


def lookup(host, *args, **kwargs):
    guard(host)
    return original_lookup(host, *args, **kwargs)


def post(api, endpoint, body):
    wall, cpu = time.perf_counter(), time.process_time()
    response = api.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    report["requests"].append(
        {
            "endpoint": endpoint,
            "question": body.get("message"),
            "seconds": time.perf_counter() - wall,
            "cpu_seconds": time.process_time() - cpu,
            "response": data,
        }
    )
    OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return data


def main():
    settings = Settings()
    assert settings.ai_provider == settings.embedding_provider == "native"
    socket.socket.connect, socket.getaddrinfo = connect, lookup
    try:
        # Prove denial before running all real model operations under the guard.
        try:
            socket.getaddrinfo("example.com", 443)
            raise AssertionError("Guard failed")
        except OSError:
            report["guard_probe_denied"] = True
        report["external_attempts"].clear()
        with tempfile.TemporaryDirectory(
            prefix="real-acceptance-", dir=ROOT / ".cache"
        ) as folder:
            settings = settings.model_copy(update={"data_dir": Path(folder)})
            provider, embeddings = (
                NativeProvider(settings, loader=loader),
                NativeEmbeddings(settings, loader=loader),
            )
            app = create_app(settings, provider, embeddings)
            with TestClient(app, base_url="http://localhost") as api:
                api.portal.call(wait_ready, provider)
                api.portal.call(wait_ready, embeddings)
                report["status"] = api.get("/api/status").json()
                assert report["status"]["status"] == "ready"
                vectors = api.portal.call(
                    embeddings.embed, ["Grafana dashboard", "database backups"]
                )
                report["embedding_dimension"] = len(vectors[0])
                assert (
                    len(vectors) == 2
                    and len(vectors[0]) == 384
                    and vectors[0] != vectors[1]
                )
                first_model = provider.model

                async def stream():
                    start, first = time.perf_counter(), None
                    chunks = []
                    async for token in provider.stream(
                        [
                            {
                                "role": "user",
                                "content": "Say hello in one short sentence.",
                            }
                        ]
                    ):
                        if first is None:
                            first = time.perf_counter() - start
                        chunks.append(token)
                    return {
                        "first_token_seconds": first,
                        "seconds": time.perf_counter() - start,
                        "chunks": len(chunks),
                        "text": "".join(chunks),
                    }

                report["stream"] = api.portal.call(stream)
                assert report["stream"]["chunks"] > 1
                post(
                    api,
                    "/api/chat",
                    {
                        "message": "What is a homelab? Answer briefly.",
                        "mode": "general",
                    },
                )
                for filename in (
                    "monitoring.md",
                    "reverse-proxy.yaml",
                    "database.json",
                ):
                    raw = (ROOT / "data/examples" / filename).read_bytes()
                    uploaded = api.post(
                        "/api/documents/upload", files={"file": (filename, raw)}
                    )
                    assert uploaded.status_code == 201, uploaded.text
                    duplicate = api.post(
                        "/api/documents/upload", files={"file": (filename, raw)}
                    )
                    assert duplicate.json()["id"] == uploaded.json()["id"]
                assert len(api.get("/api/documents").json()) == 3
                answer = post(
                    api,
                    "/api/chat",
                    {
                        "message": "Answer in one sentence ending with [S1]. What TCP port does Grafana use?"
                    },
                )
                assert (
                    "3000" in answer["answer"]
                    and "[S1]" in answer["answer"]
                    and answer["sources"]
                )
                for source in answer["sources"]:
                    lines = (
                        (ROOT / "data/examples" / source["filename"])
                        .read_text()
                        .splitlines()
                    )
                    assert source["text"] == "\n".join(
                        lines[source["line_start"] - 1 : source["line_end"]]
                    )
                absent = post(
                    api, "/api/chat", {"message": "Who won the lunar chess cup?"}
                )
                assert not absent["grounded"] and not absent["sources"]
                stream_response = api.post(
                    "/api/chat", json={"message": "Grafana port?", "stream": True}
                )
                events = [
                    json.loads(line) for line in stream_response.text.splitlines()
                ]
                report["rag_stream"] = events
                assert events[-1]["type"] == "done" and any(
                    e.get("content") for e in events
                )
                log = (ROOT / "data/examples/proxy-incident.log").read_text()
                body = {
                    "content": log,
                    "service": "reverse-proxy",
                    "use_model": True,
                    "question": "Briefly explain symptoms, cite lines and acknowledge uncertainty.",
                }
                analysis = post(api, "/api/logs/analyse", body)
                assert analysis["error_count"] == 3 and analysis["explanation"]
                incident = post(
                    api,
                    "/api/incidents/summarise",
                    {**body, "title": "Fictional proxy incident"},
                )
                assert (
                    incident["analysis"]["explanation"]
                    and "192.0.2.20:3000" in incident["markdown"]
                )
                assert provider.model is first_model and len(report["loads"]) == 2
                report["reuse_verified"] = True
                report["rss_after_workflows"] = memory()
            # Recreate models and reconstruct the vector index from persisted real embeddings.
            app = create_app(settings)
            with TestClient(app, base_url="http://localhost") as api:
                api.portal.call(wait_ready, app.state.provider)
                api.portal.call(wait_ready, app.state.embeddings)
                answer = post(
                    api,
                    "/api/chat",
                    {
                        "message": "Answer in one sentence ending with [S1]. What TCP port does Grafana use?"
                    },
                )
                assert "3000" in answer["answer"] and answer["sources"]
                report["restart_retrieval_verified"] = True
                doc = next(
                    x
                    for x in api.get("/api/documents").json()
                    if x["filename"] == "database.json"
                )
                assert api.delete("/api/documents/" + doc["id"]).status_code == 204
                assert (
                    api.get("/api/search", params={"q": "5432"}).json()["chunks"] == []
                )
                report["deletion_verified"] = True
        assert not report["external_attempts"]
        report["result"] = "PASS"
    finally:
        socket.socket.connect, socket.getaddrinfo = original_connect, original_lookup
        OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"requests", "rag_stream"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
