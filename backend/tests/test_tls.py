import asyncio
import ipaddress
import json
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import cli
from app.models import ChatRequest, ServiceInput
from app.services import tls
from app.services.live import live_answer
from app.services.runtime import embedding_identity
from app.services.storage import Storage


@pytest.fixture
def certs(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    fixtures = {}
    for name, start, end in (
        ("valid", -1, 90),
        ("expired", -90, -1),
        ("future", 1, 90),
        ("soon", -1, 12),
    ):
        subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=start))
            .not_valid_after(now + timedelta(days=end))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                        x509.DNSName("*.home.arpa"),
                        x509.RFC822Name("admin@example.test"),
                        x509.UniformResourceIdentifier("https://example.test/identity"),
                    ]
                ),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        pem = tmp_path / f"{name}.pem"
        pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private = tmp_path / f"{name}.key"
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pem, private)
        trusted = ssl.create_default_context(cafile=str(pem))
        trusted.check_hostname = False
        fixtures[name] = (cert.public_bytes(serialization.Encoding.DER), context, trusted)
    return fixtures


@asynccontextmanager
async def server(context=None, stall=False):
    async def handle(reader, writer):
        try:
            if stall:
                await reader.read(65536)
                await reader.read(1)
            else:
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()

    instance = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        yield instance.sockets[0].getsockname()[1]
    finally:
        instance.close()
        await instance.wait_closed()


@pytest.mark.parametrize(
    "name,validity",
    [("valid", "valid"), ("expired", "expired"), ("future", "not-yet-valid"), ("soon", "valid")],
)
def test_certificate_dates_and_sans(certs, name, validity):
    facts = tls.certificate_facts(certs[name][0], "localhost", datetime.now(UTC))
    assert facts["validity"] == validity
    assert facts["hostname_match"] == "match"
    assert facts["sans"][1] == {"type": "IP Address", "value": "127.0.0.1"}
    assert {"type": "RFC822Name", "value": "admin@example.test"} in facts["sans"]
    assert {"type": "UniformResourceIdentifier", "value": "https://example.test/identity"} in facts[
        "sans"
    ]
    assert facts["subject"] and facts["issuer"] and facts["serial_number"]
    assert len(facts["fingerprint_sha256"]) == 64
    assert facts["expiring_soon"] == (name == "soon")


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", "match"),
        ("127.0.0.1", "match"),
        ("grafana.home.arpa", "match"),
        ("deep.grafana.home.arpa", "mismatch"),
        ("127.0.0.2", "mismatch"),
        ("wrong.example", "mismatch"),
    ],
)
def test_hostname(certs, host, expected):
    assert (
        tls.certificate_facts(certs["valid"][0], host, datetime.now(UTC))["hostname_match"]
        == expected
    )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("valid", "valid"),
        ("expired", "expired"),
        ("future", "not-yet-valid"),
        ("soon", "expiring-soon"),
    ],
)
async def test_real_trusted(certs, name, expected):
    _, context, trust = certs[name]
    async with server(context) as port:
        item = await tls.inspect_tls("127.0.0.1", port, trust_context=trust)
    assert item["state"] == expected, item
    assert item["connection_success"] and item["certificate_presented"]
    assert item["protocol"].startswith("TLS") and item["cipher"]
    assert item["trust"] == ("trusted" if name in {"valid", "soon"} else "failed")


async def test_real_untrusted_and_hostname_mismatch(certs, monkeypatch):
    async with server(certs["valid"][1]) as port:
        item = await tls.inspect_tls("127.0.0.1", port)
        assert item["state"] == "untrusted" and item["connection_success"]
        assert item["hostname_match"] == "match" and item["trust_verify_code"]

        async def lookup(host):
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(tls, "system_lookup", lookup)
        mismatch = await tls.inspect_tls("wrong.example", port, trust_context=certs["valid"][2])
        assert mismatch["state"] == "hostname-mismatch"
        assert mismatch["trust"] == "trusted"


async def test_real_plaintext_and_timeout():
    async with server() as port:
        item = await tls.inspect_tls("127.0.0.1", port)
        assert item["state"] == "handshake-failed"
        assert not item["certificate_presented"]
    async with server(stall=True) as port:
        item = await tls.inspect_tls("127.0.0.1", port, timeout=0.03)
        assert item["error"] == "timeout" and item["state"] == "handshake-failed"
    item = await tls.inspect_tls("127.0.0.1", port)
    assert item["state"] == "handshake-failed"


@pytest.mark.parametrize(
    "host,port",
    [
        ("169.254.169.254", 443),
        ("::", 443),
        ("224.0.0.1", 443),
        ("fd00:ec2::254", 443),
        ("bad host", 443),
        ("localhost", 0),
    ],
)
async def test_safety(host, port):
    item = await tls.inspect_tls(host, port)
    assert item["error"] in {"blocked_target", "invalid"}
    assert not item["connection_success"]


async def test_mixed_resolution_refused(monkeypatch):
    async def lookup(host):
        return [(2, 1, 6, "", (v, 0)) for v in ["127.0.0.1", "169.254.169.254"]]

    monkeypatch.setattr(tls, "system_lookup", lookup)
    assert (await tls.inspect_tls("local.test"))["error"] == "blocked_target"


def configured(store, url="https://localhost:3000"):
    return store.save_service(ServiceInput(name="Grafana", kind="other", monitor={"url": url}))


async def test_history_evidence_and_cli(settings, monkeypatch, capsys, certs):
    store = Storage(settings.data_dir, embedding_identity(settings))
    service = configured(store)
    facts = tls.certificate_facts(certs["valid"][0], "localhost", datetime.now(UTC))

    async def inspect(*args, **kwargs):
        return dict(
            **facts,
            hostname="localhost",
            port=3000,
            state="untrusted",
            trust="failed",
            reason="Certificate presented",
            observed_at=datetime.now(UTC).isoformat(),
            latency_ms=1,
        )

    monkeypatch.setattr(tls, "inspect_tls", inspect)
    item = await tls.inspect_and_record(store, service)
    assert (await tls.inspect_and_record(store, service))["id"] == item["id"]
    old = {
        **item,
        "id": str(uuid4()),
        "issuer": "Old CA",
        "fingerprint_sha256": "old",
        "expires_at": "2020-01-01T00:00:00+00:00",
        "trust": "trusted",
    }
    old["checked_at"] = old["observed_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    store.record_tls(old)
    assert tls.summary(store, service)["changes"]["fingerprint_sha256"]
    for question in [
        "When does the Grafana certificate expire?",
        "Which HTTPS services have certificates expiring soon?",
        "Does the Grafana certificate match the hostname?",
        "Has this Grafana certificate changed since the last check?",
    ]:
        response = await live_answer(ChatRequest(message=question, probe=True), store)
        evidence = response["live"][0]
        assert evidence.evidence_type == "tls_observation" and evidence.tls["id"] == item["id"]
        assert "LIVE TLS OBSERVATION" in response["answer"]
        assert response["grounded"] and f"[{evidence.label}]" in response["answer"]
    assert not store.observations(service["id"])  # certificate-only questions never HTTP probe
    for args in [["services", "tls", "Grafana", "--json"], ["services", "tls"]]:
        parsed = cli.parser().parse_args(args)
        result, code = await cli.execute(parsed, settings)
        assert code == 0
        cli.emit(result, parsed.json)
        output = capsys.readouterr().out
        if parsed.json:
            assert json.loads(output)["tls_results"][0]["id"] == item["id"]
        else:
            assert "Status: UNTRUSTED" in output
    for i in range(55):
        store.record_tls({**item, "id": str(i)})
    assert len(store.tls_observations(service["id"])) == 50
    store.close()
    store = Storage(settings.data_dir, embedding_identity(settings))
    assert len(store.tls_observations(service["id"])) == 50
    store.delete_service(service["id"])
    assert not store.tls_observations(service["id"])
    store.close()


def test_api(client, monkeypatch):
    async def inspect(*args, **kwargs):
        return dict(
            hostname="localhost",
            port=3000,
            state="untrusted",
            reason="Certificate presented",
            observed_at=datetime.now(UTC).isoformat(),
            latency_ms=1,
        )

    monkeypatch.setattr(tls, "inspect_tls", inspect)
    service = client.post(
        "/api/services",
        json={"name": "Grafana", "kind": "other", "monitor": {"url": "https://localhost:3000"}},
    ).json()
    path = f"/api/services/{service['id']}/tls"
    assert client.get(path).json()["latest"] is None
    item = client.post(path).json()
    assert item["state"] == "untrusted"
    assert client.get(path).json()["latest"]["id"] == item["id"]
    assert client.get("/api/services?health=true").json()[0]["tls"]["latest"]["id"] == item["id"]
    assert client.post("/api/services/00000000-0000-0000-0000-000000000000/tls").status_code == 404
    response = client.post(
        "/api/chat", json={"message": "When does Grafana certificate expire?"}
    ).json()
    assert response["live"][0]["evidence_type"] == "tls_observation"


async def test_skip_and_batch(settings):
    store = Storage(settings.data_dir, embedding_identity(settings))
    service = configured(store, "http://localhost")
    assert tls.target(service) is None
    assert not (await tls.inspect_and_record(store, service))["applicable"]
    assert await tls.inspect_many(store, [service]) == []
    with pytest.raises(ValueError, match="50"):
        await tls.inspect_many(store, [{"monitor": {"url": "https://localhost"}}] * 51)
    store.close()


def test_bounded_and_malformed_certificate(certs):
    for der in [b"bad DER", b"x" * (tls.MAX_CERT_BYTES + 1)]:
        with pytest.raises(ValueError):
            tls.certificate_facts(der, "localhost", datetime.now(UTC))


async def test_changed_between_handshakes_and_failed_validation(certs, monkeypatch):
    calls = []

    async def exchange(*args):
        calls.append(args)
        return (
            certs["valid" if len(calls) % 2 else "soon"][0],
            "TLSv1.3",
            ("cipher", "TLSv1.3", 256),
        )

    monkeypatch.setattr(tls, "exchange", exchange)
    item = await tls.inspect_tls("127.0.0.1")
    assert item["trust"] == "unavailable" and item["state"] == "unknown"
    assert "changed" in item["trust_reason"]
    assert calls[0][0] == calls[1][0]  # both connections pinned to same address

    async def fail(*args):
        if args[-1].verify_mode == ssl.CERT_REQUIRED:
            raise OSError("remote message must not be retained")
        return certs["valid"][0], "TLSv1.3", ("cipher", "TLSv1.3", 256)

    monkeypatch.setattr(tls, "exchange", fail)
    item = await tls.inspect_tls("127.0.0.1")
    assert item["connection_success"] and item["certificate_presented"]
    assert item["trust"] == "unavailable" and item["validity"] == "valid"
    assert "remote message" not in json.dumps(item)


async def test_resolution_deadline_and_bounds(monkeypatch):
    async def slow(host):
        await asyncio.sleep(1)

    monkeypatch.setattr(tls, "system_lookup", slow)
    item = await tls.inspect_tls("local.test", timeout=0.01)
    assert item["error"] == "timeout"

    async def huge(host):
        return [(2, 1, 6, "", ("127.0.0.1", 0))] * 33

    monkeypatch.setattr(tls, "system_lookup", huge)
    assert (await tls.inspect_tls("local.test"))["error"] == "blocked_target"


async def test_global_retention_and_comparison_scope(settings):
    store = Storage(settings.data_dir, embedding_identity(settings))
    service = configured(store)
    now = datetime.now(UTC).isoformat()
    item = dict(
        id="first",
        service_id=service["id"],
        checked_at=now,
        observed_at=now,
        hostname="localhost",
        port=443,
        fingerprint_sha256="a",
    )
    store.record_tls(item)
    store.record_tls({**item, "id": "second", "hostname": "other.test"})
    assert tls.summary(store, service)["changes"] is None
    targets = [
        store.save_service(ServiceInput(name=f"TLS seed{i}", kind="other")) for i in range(101)
    ]
    with store.connect() as db:
        db.executemany(
            "INSERT INTO tls_observations VALUES (?,?,?,?)",
            [
                (f"{s['id']}-{i}", s["id"], now, json.dumps(item))
                for s in targets
                for i in range(50)
            ],
        )
    store.record_tls({**item, "id": "last"})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM tls_observations").fetchone()[0] == 5000
    store.close()


async def test_dns_failure_is_not_handshake_failure(monkeypatch):
    import socket

    async def missing(host):
        raise socket.gaierror(socket.EAI_NONAME, "untrusted error")

    monkeypatch.setattr(tls, "system_lookup", missing)
    item = await tls.inspect_tls("missing.test")
    assert item["error"] == "dns_failure" and item["state"] == "unknown"
    assert not item["connection_success"] and item["trust"] == "unavailable"
    assert "untrusted error" not in json.dumps(item)


def test_missing_names_are_unavailable():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(x509.Name([]))
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    facts = tls.certificate_facts(cert.public_bytes(serialization.Encoding.DER), "localhost", now)
    assert facts["hostname_match"] == "unavailable" and facts["sans"] == []


async def test_capacity_is_bounded_across_callers(monkeypatch):
    import threading

    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(tls, "_slots", slots)
    try:
        item = await tls.inspect_tls("127.0.0.1")
        assert item["error"] == "busy" and not item["connection_success"]
    finally:
        slots.release()
