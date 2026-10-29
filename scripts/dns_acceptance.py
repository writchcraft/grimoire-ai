"""Opt-in local DNS/HTTP acceptance using disposable inventory; no public-DNS dependency."""

import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import cli
from app.core.config import Settings
from app.main import create_app
from app.services.dns import inspect_hostname
from fastapi.testclient import TestClient


def main():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = {
            "localhost": asyncio.run(inspect_hostname("localhost")),
            "invalid": asyncio.run(
                inspect_hostname("grimoire-dns-acceptance.invalid.")
            ),
            "lan": "Not tested: configured local inventory contained no services or LAN hostnames.",
            "scope": "Real system resolver and loopback HTTP; disposable configured localhost service. "
            "No public internet names; OS networking was not disabled.",
        }
        assert report["localhost"]["status"] == "resolved"
        assert report["invalid"]["status"] in {"unresolved", "timeout"}
        with tempfile.TemporaryDirectory(prefix="grimoire-dns-") as directory:
            settings = Settings(data_dir=Path(directory))
            with TestClient(
                create_app(settings), base_url="http://localhost"
            ) as client:
                service = client.post(
                    "/api/services",
                    json={
                        "name": "Local acceptance",
                        "kind": "other",
                        "monitor": {"url": f"http://localhost:{server.server_port}"},
                    },
                ).json()
                path = f"/api/services/{service['id']}"
                report["configured_service_dns"] = client.post(path + "/dns").json()
                report["http"] = client.post(path + "/probe").json()
                report["chat"] = client.post(
                    "/api/chat",
                    json={
                        "message": "Why might Local acceptance be unreachable?",
                    },
                ).json()
                report["cli"], code = asyncio.run(
                    cli.execute(
                        cli.parser().parse_args(
                            ["services", "dns", "Local acceptance", "--json"]
                        ),
                        settings,
                    )
                )
                assert code == 0
                assert report["configured_service_dns"]["status"] == "resolved"
                assert report["http"]["id"]
                literal = client.post(
                    "/api/services",
                    json={
                        "name": "Literal loopback acceptance",
                        "kind": "other",
                        "monitor": {"url": f"http://127.0.0.1:{server.server_port}"},
                    },
                ).json()
                report["literal_http"] = client.post(
                    f"/api/services/{literal['id']}/probe"
                ).json()
                assert report["literal_http"]["state"] == "healthy"
                report["correlation_note"] = (
                    "Localhost can resolve successfully while the existing HTTP safety policy "
                    "refuses the IPv6 loopback address. HTTP address validation was preserved."
                )
                assert {e["evidence_type"] for e in report["chat"]["live"]} == {
                    "dns_observation",
                    "http_observation",
                }
        report["result"] = "PASS"
        output = Path("docs/dns-evidence/local.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"PASS: {output}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
