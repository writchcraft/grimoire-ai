"""Real loopback TLS/API/CLI acceptance. Generated keys live only in a temporary directory."""

import asyncio
import ipaddress
import json
import sqlite3
import ssl
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import cli
from app.core.config import Settings
from app.main import create_app
from app.services import tls
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    report = {
        "scope": "Real loopback TLS sockets; generated disposable certificates; no public endpoints."
    }
    inventories = []
    for path in Path("data").rglob("*.sqlite3"):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            try:
                inventories.extend(
                    json.loads(r[0]) for r in db.execute("SELECT data FROM services")
                )
            except sqlite3.OperationalError:
                pass
    report["configured_https_count"] = sum(bool(tls.target(s)) for s in inventories)
    report["lan"] = (
        "No configured HTTPS service available; real LAN TLS not tested."
        if not report["configured_https_count"]
        else "Configured targets not contacted by this disposable acceptance script."
    )
    with tempfile.TemporaryDirectory(prefix="grimoire-tls-") as directory:
        root = Path(directory)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        keyfile = root / "key.pem"
        keyfile.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        for name, start, end, host in [
            ("valid", -1, 90, "127.0.0.1"),
            ("untrusted", -1, 90, "127.0.0.1"),
            ("mismatch", -1, 90, "127.0.0.2"),
            ("expired", -90, -1, "127.0.0.1"),
            ("not_yet_valid", 1, 90, "127.0.0.1"),
            ("expiring_soon", -1, 12, "127.0.0.1"),
        ]:
            now = datetime.now(UTC)
            identity = x509.Name(
                [x509.NameAttribute(x509.NameOID.COMMON_NAME, "Local acceptance")]
            )
            cert = (
                x509.CertificateBuilder()
                .subject_name(identity)
                .issuer_name(identity)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now + timedelta(days=start))
                .not_valid_after(now + timedelta(days=end))
                .add_extension(
                    x509.SubjectAlternativeName(
                        [x509.IPAddress(ipaddress.ip_address(host))]
                    ),
                    False,
                )
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                .sign(key, hashes.SHA256())
            )
            certfile = root / "cert.pem"
            certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            trust = ssl.create_default_context(cafile=str(certfile))
            trust.check_hostname = False
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                item = asyncio.run(
                    tls.inspect_tls(
                        "127.0.0.1",
                        server.server_port,
                        trust_context=None if name == "untrusted" else trust,
                    )
                )
                expected = {
                    "mismatch": "hostname-mismatch",
                    "not_yet_valid": "not-yet-valid",
                    "expiring_soon": "expiring-soon",
                }.get(name, name)
                assert item["state"] == expected, item
                report[name] = item
                if name == "untrusted":
                    settings = Settings(data_dir=root / "runtime")
                    with TestClient(
                        create_app(settings), base_url="http://localhost"
                    ) as client:
                        service = client.post(
                            "/api/services",
                            json={
                                "name": "Local TLS",
                                "kind": "other",
                                "monitor": {
                                    "url": f"https://127.0.0.1:{server.server_port}"
                                },
                            },
                        ).json()
                        path = f"/api/services/{service['id']}"
                        report["api"] = client.post(path + "/tls").json()
                        assert report["api"]["state"] == "untrusted"
                        report["http"] = client.post(path + "/probe").json()
                        assert report["http"]["state"] == "unreachable"
                        report["chat"] = client.post(
                            "/api/chat", json={"message": "Why is Local TLS failing?"}
                        ).json()
                        assert any(
                            e["evidence_type"] == "tls_observation"
                            for e in report["chat"]["live"]
                        )
                        report["cli"], code = asyncio.run(
                            cli.execute(
                                cli.parser().parse_args(
                                    ["services", "tls", "Local TLS", "--json"]
                                ),
                                settings,
                            )
                        )
                        assert (
                            code == 0
                            and report["cli"]["tls_results"][0]["id"]
                            == report["api"]["id"]
                        )
            finally:
                server.shutdown()
                server.server_close()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            report["plaintext"] = asyncio.run(
                tls.inspect_tls("127.0.0.1", server.server_port)
            )
            assert report["plaintext"]["state"] == "handshake-failed"
        finally:
            server.shutdown()
            server.server_close()
        report["refused"] = asyncio.run(
            tls.inspect_tls("127.0.0.1", server.server_port)
        )
        assert report["refused"]["state"] == "handshake-failed"
    destination = Path("docs/tls-evidence/local.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: v.get("state") if isinstance(v, dict) else v
                for k, v in report.items()
                if k not in {"chat", "cli"}
            }
        )
    )


if __name__ == "__main__":
    main()
