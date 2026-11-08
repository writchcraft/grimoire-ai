"""Real Windows acceptance: disposable loopback listeners only; no network changes."""

import asyncio
import json
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import cli
from app.core.config import Settings
from app.main import create_app
from app.models import ChatRequest, MonitorConfig, ServiceInput
from app.services import tcp
from app.services.live import live_answer
from app.services.runtime import embedding_identity
from app.services.storage import Storage
from fastapi.testclient import TestClient


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class UnusedModel:
    async def start(self):
        pass

    async def close(self):
        pass


async def run():
    report = {"scope": "Disposable loopback endpoints only; no infrastructure changes"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        report["ipv4"] = await tcp.inspect_tcp("127.0.0.1", port)
        report["hostname"] = await tcp.inspect_tcp("localhost", port)
        assert report["ipv4"]["state"] == report["hostname"]["state"] == "reachable"
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            store = Storage(settings.data_dir, embedding_identity(settings))
            try:
                service = store.save_service(
                    ServiceInput(
                        name="Local Grimoire endpoint",
                        kind="other",
                        hostname="localhost",
                        ports=[port],
                        monitor=MonitorConfig(url=f"http://localhost:{port}"),
                    )
                )
                report["configured"] = await tcp.inspect_and_record(store, service)
                report["cooldown"] = await tcp.inspect_and_record(store, service)
                assert report["configured"] == report["cooldown"]
                answer = await live_answer(
                    ChatRequest(
                        message="Why is Local Grimoire endpoint unavailable?",
                        probe=True,
                    ),
                    store,
                )
                report["correlation"] = answer["answer"]
                assert "TCP and HTTP checks succeeded" in answer["answer"]
                report["history"] = tcp.summary(store, service)
            finally:
                store.close()

            def api_cli():
                with TestClient(
                    create_app(settings, UnusedModel(), UnusedModel()),
                    base_url="http://localhost",
                ) as client:
                    path = f"/api/services/{service['id']}/tcp"
                    observed = client.post(path)
                    assert observed.status_code == 200, observed.text
                    assert client.get(path).json()["latest"]
                result, code = asyncio.run(
                    cli.execute(
                        cli.parser().parse_args(
                            ["services", "tcp", service["name"], "--json"]
                        ),
                        settings,
                    )
                )
                assert code == 0 and result["tcp_results"][0]["connection_success"]
                return {"api": observed.json(), "cli": result}

            report.update(await asyncio.to_thread(api_cli))
        try:
            ipv6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            ipv6.bind(("::1", 0))
            ipv6.listen(1)
        except OSError as exc:
            report["ipv6"] = {"state": "unavailable", "errno": exc.errno}
        else:
            try:
                report["ipv6"] = await tcp.inspect_tcp("::1", ipv6.getsockname()[1])
                assert report["ipv6"]["state"] == "reachable", report["ipv6"]
            finally:
                ipv6.close()
        # A tiny unaccepted local backlog safely reproduces a connect timeout where OS permits.
        held = []
        with socket.socket() as backlog:
            backlog.bind(("127.0.0.1", 0))
            backlog.listen(1)
            for _ in range(4):
                pending = socket.socket()
                pending.setblocking(False)
                held.append(pending)
                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().sock_connect(
                            pending, backlog.getsockname()
                        ),
                        0.25,
                    )
                except (TimeoutError, OSError):
                    break
            try:
                report["timeout"] = await tcp.inspect_tcp(
                    "127.0.0.1", backlog.getsockname()[1], timeout=0.25
                )
                report["timeout_reproduced"] = report["timeout"]["state"] == "timeout"
            finally:
                for pending in held:
                    pending.close()
    finally:
        server.shutdown()
        server.server_close()
    report["refused"] = await tcp.inspect_tcp("127.0.0.1", port)
    assert report["refused"]["state"] == "refused"
    destination = Path("docs/tcp-evidence/local.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "scope",
                    "ipv4",
                    "hostname",
                    "ipv6",
                    "refused",
                    "timeout_reproduced",
                )
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
