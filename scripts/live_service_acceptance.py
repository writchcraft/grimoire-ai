"""Real local TCP acceptance, using disposable Grimoire state and no model/fake providers."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/monitoring-evidence/local.json")
    )
    args = parser.parse_args()
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GRIMOIRE_")}
    with tempfile.TemporaryDirectory(prefix="grimoire-live-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment.update(GRIMOIRE_PORT=str(port), GRIMOIRE_DATA_DIR=directory)
        with (Path(directory) / "server.log").open("w", encoding="utf-8") as logs:
            server = subprocess.Popen(
                [sys.executable, "-m", "app.cli", "serve"],
                env=environment,
                stdout=logs,
                stderr=logs,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
                ) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if server.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Disposable Grimoire did not start")
                        time.sleep(0.2)
                    target = client.post(
                        "/api/services",
                        json={
                            "name": "Grimoire local acceptance",
                            "kind": "other",
                            "tags": ["local", "acceptance"],
                            "monitor": {
                                "url": f"http://127.0.0.1:{port}/health",
                                "timeout_seconds": 5,
                            },
                        },
                    )
                    target.raise_for_status()
                    service_id = target.json()["id"]
                    observation = client.post(f"/api/services/{service_id}/probe")
                    observation.raise_for_status()
                    assert observation.json()["state"] == "healthy"
                    ready_target = client.post(
                        "/api/services",
                        json={
                            "name": "Grimoire dependency readiness",
                            "kind": "other",
                            "monitor": {
                                "url": f"http://127.0.0.1:{port}/health",
                                "health_url": f"http://127.0.0.1:{port}/ready",
                            },
                        },
                    ).json()
                    readiness = client.post(
                        f"/api/services/{ready_target['id']}/probe"
                    ).json()
                    assert readiness["state"] == "degraded"
                    assert readiness["health_endpoint"]["status_code"] == 503
                    chat = client.post(
                        "/api/chat",
                        json={
                            "message": "Is Grimoire local acceptance currently online?",
                        },
                    )
                    chat.raise_for_status()
                    assert (
                        chat.json()["live"][0]["observation_id"]
                        == observation.json()["id"]
                    )
                    commands = []
                    for command in (
                        ["services", "list", "--json"],
                        ["services", "probe", "Grimoire local acceptance", "--json"],
                        ["services", "status", "--json"],
                        ["chat", "Summarise current service health.", "--json"],
                    ):
                        completed = subprocess.run(
                            [sys.executable, "-m", "app.cli", *command],
                            check=False,
                            env=environment,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        assert completed.returncode == 0, completed.stderr
                        commands.append(
                            {"command": command, "result": json.loads(completed.stdout)}
                        )
                    report = {
                        "accepted_at": datetime.now(UTC).isoformat(),
                        "result": "PASS",
                        "scope": "Real loopback TCP to a disposable Grimoire process; no fake providers. "
                        "Models intentionally unconfigured; /ready correctly returns 503. "
                        "No external endpoints requested; external networking not OS-blocked.",
                        "healthy_observation": observation.json(),
                        "degraded_readiness": readiness,
                        "chat": chat.json(),
                        "cli": commands,
                    }
            finally:
                if os.name == "nt" and server.poll() is None:
                    # Windows venv Python may launch a child interpreter. Stop our whole tree.
                    subprocess.run(
                        ["taskkill", "/PID", str(server.pid), "/T", "/F"],
                        check=True,
                        capture_output=True,
                    )
                    server.wait(timeout=10)
                elif server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
    report["server_stopped"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"PASS: real local API, probe, readiness, chat and CLI; {args.output}")


if __name__ == "__main__":
    main()
