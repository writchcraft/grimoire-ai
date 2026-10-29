"""Opt-in real TCP/CLI crash-restart rehearsal; uses only disposable state and local weights."""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from app.core.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".cache/deployment/service.json")
    )
    args = parser.parse_args()
    settings = Settings()
    if settings.ai_provider != "native" or settings.embedding_provider != "native":
        raise RuntimeError("This rehearsal requires provisioned local native models.")
    report = {
        "platform": platform.platform(),
        "cycles": [],
        "commands": [],
        "network_scope": "Real loopback TCP; outbound networking not blocked in this rehearsal",
    }
    environment = os.environ.copy()
    for name, path in (
        ("GRIMOIRE_NATIVE_MODEL_PATH", settings.native_model_path),
        ("GRIMOIRE_NATIVE_EMBEDDING_MODEL_PATH", settings.native_embedding_model_path),
    ):
        if path is None:
            raise RuntimeError("Both native weight paths are required.")
        environment[name] = str(path.resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="grimoire-service-") as directory:
        environment["GRIMOIRE_DATA_DIR"] = str(Path(directory) / "state")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment["GRIMOIRE_PORT"] = str(port)
        question = {
            "message": "What port does Grafana use?",
            "mode": "knowledge",
            "stream": False,
        }
        original = document = None
        for cycle in range(2):
            with (Path(directory) / f"server-{cycle}.txt").open(
                "w", encoding="utf-8"
            ) as logs:
                process = subprocess.Popen(
                    [sys.executable, "-m", "app.cli", "serve"],
                    env=environment,
                    stdout=logs,
                    stderr=logs,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        timeout=180,
                        trust_env=False,
                    ) as client:
                        deadline = time.monotonic() + 120
                        while True:
                            if process.poll() is not None:
                                raise RuntimeError(
                                    "Serve exited before acceptance; inspect configuration."
                                )
                            try:
                                status = client.get("/api/status")
                                if (
                                    status.status_code == 200
                                    and status.json()["embeddings"]["state"] == "ready"
                                ):
                                    break
                            except httpx.TransportError:
                                pass
                            if time.monotonic() > deadline:
                                raise RuntimeError("Serve readiness deadline exceeded.")
                            time.sleep(0.2)
                        assert client.get("/").status_code == 200
                        assert client.get("/src/app.js").status_code == 200
                        if cycle == 0:
                            response = client.post(
                                "/api/documents/ingest",
                                json={
                                    "filename": "grafana.md",
                                    "content": "# Grafana\nGrafana uses port 3000.\n",
                                },
                            )
                            assert response.status_code == 201, response.text
                            document = response.json()["id"]
                            assert (
                                client.post(
                                    "/api/services",
                                    json={"name": "grafana", "kind": "monitoring"},
                                ).status_code
                                == 201
                            )
                            assert (
                                client.post(
                                    "/api/incidents/summarise",
                                    json={"content": "ERROR timeout"},
                                ).status_code
                                == 200
                            )
                        assert client.get("/api/documents").json()[0]["id"] == document
                        assert (
                            client.get("/api/services").json()[0]["name"] == "grafana"
                        )
                        assert len(client.get("/api/incidents").json()) == 1
                        assert client.get("/api/search", params={"q": "3000"}).json()[
                            "chunks"
                        ]
                        response = client.post("/api/chat", json=question)
                        assert response.status_code == 200, response.text
                        answer = response.json()
                        assert answer["grounded"] and "3000" in answer["answer"]
                        if original is None:
                            original = answer
                        else:
                            assert answer["sources"] == original["sources"]
                            assert answer["answer"] == original["answer"]
                        assert client.get("/ready").status_code == 200
                        report["cycles"].append(
                            {
                                "cycle": cycle,
                                "answer": answer["answer"],
                                "citations_identical": True,
                            }
                        )
                finally:
                    if process.poll() is None:
                        # Windows terminate() is also abrupt; Linux second stop uses SIGTERM.
                        process.kill() if cycle == 0 else process.terminate()
                    process.wait(timeout=120)
        report["first_stop"] = (
            "abrupt process kill; successful real-server restart and cited RAG"
        )
        report["second_stop"] = (
            "abrupt Windows termination" if os.name == "nt" else "SIGTERM"
        )
        for command in (["doctor"], ["status"], ["models", "list"], ["validate"]):
            result = subprocess.run(
                [sys.executable, "-m", "app.cli", *command, "--json"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert result.returncode == 0, result.stdout
            report["commands"].append(
                {
                    "command": "grimoire " + " ".join(command),
                    "exit_code": result.returncode,
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "result": "PASS real serve process crash/restart on reported platform",
                "report": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
