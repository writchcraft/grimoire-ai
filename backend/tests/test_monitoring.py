import asyncio
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app import cli
from app.models import ChatRequest, ServiceInput
from app.services.live import live_answer, plan_live
from app.services.live import probe_and_record as probe
from app.services.probe import check_url as validate_url
from app.services.probe import derive_state, probe_endpoint
from app.services.runtime import embedding_identity
from app.services.storage import Storage

HISTORY_LIMIT = 50


async def endpoint(url, expected, seconds):
    return await probe_endpoint(url, seconds)


@pytest.fixture
def http_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == "/slow":
                time.sleep(0.4)
            status = 503 if self.path == "/fail" else 200
            body = b"untrusted body NEVER STORE"
            if self.path in {"/redirect", "/loop", "/cross", "/cookie", "/metadata"}:
                self.send_response(302)
                self.send_header(
                    "Location",
                    {
                        "/redirect": "/ok",
                        "/loop": "/loop",
                        "/cookie": "/ok",
                        "/cross": "http://127.0.0.1:1/secret",
                        "/metadata": "http://169.254.169.254/latest/meta-data/",
                    }[self.path],
                )
                self.send_header("Set-Cookie", "secret=do-not-send")
                self.end_headers()
                return
            self.send_response(status)
            if self.path == "/large":
                self.send_header("Content-Length", "1000000")
            self.end_headers()
            if self.path == "/stream-large":
                body = b"a" * 100000
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize(
    "path,status,error",
    [
        ("/ok", 200, None),
        ("/fail", 503, None),
        ("/redirect", 200, None),
        ("/loop", 302, "too_many_redirects"),
        ("/cross", 302, "cross_origin_redirect"),
        ("/metadata", 302, "blocked_target"),
        ("/large", 200, "response_too_large"),
        ("/stream-large", 200, "response_too_large"),
        ("/slow", None, "timeout"),
    ],
)
async def test_endpoint(http_server, path, status, error):
    url, requests = http_server
    result = await endpoint(url + path, 200, 0.1 if path == "/slow" else 2)
    assert result["status_code"] == status
    assert result["error_kind"] == error
    assert result["latency_ms"] >= 0
    assert "untrusted body" not in json.dumps(result)
    assert len(requests) <= 4
    if path == "/redirect":
        assert result["final_url"] == url + "/ok"
        assert result["redirects"] == 1


async def test_no_proxy_credentials_or_cookies(http_server, monkeypatch):
    url, requests = http_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    result = await endpoint(url + "/cookie", 200, 2)
    assert result["status_code"] == 200
    for _, headers in requests:
        assert not {"Authorization", "Cookie", "Proxy-Authorization"} & headers.keys()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://localhost/a",
        "http://user:secret@localhost/",
        "http://localhost/?token=secret",
        "http://localhost/#x",
        "http://localhost:99999/",
        "http://169.254.169.254/latest",
        "http://[fe80::1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://100.100.100.200/",
        "http://localhost/\r\nx",
        "http://localhost\\evil/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_invalid_url(url):
    with pytest.raises(ValueError):
        validate_url(url)
    with pytest.raises(ValueError):
        ServiceInput(name="test", kind="other", monitor={"url": url})


async def test_connection_failure():
    def refused(request):
        raise httpx.ConnectError("refused") from ConnectionRefusedError()

    result = await probe_endpoint("http://127.0.0.1:1/", transport=httpx.MockTransport(refused))
    assert result["error_kind"] in {"connection_failed", "connection_refused"}
    assert derive_state({"expected_status": 200}, result, None)[0] == "unreachable"


async def test_dns_validated_and_pinned(http_server, monkeypatch):
    url, requests = http_server
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    calls = []

    async def resolve(host, port, **kwargs):
        calls.append(host)
        return await original("127.0.0.1", port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    target = url.replace("127.0.0.1", "homelab.example")
    result = await endpoint(target + "/redirect", 200, 2)
    assert result["status_code"] == 200
    assert calls == ["homelab.example"]
    assert requests[-1][1]["Host"].startswith("homelab.example:")


async def test_dns_metadata_blocked(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    result = await endpoint("http://metadata.example/", 200, 1)
    assert result["error_kind"] == "blocked_target"
    assert result["reachable"] is False


def test_state_mapping():
    monitor = {"expected_status": 200, "slow_ms": 2000}
    good = {"reachable": True, "status_code": 200, "error": None, "latency_ms": 10}
    bad = {**good, "status_code": 503}
    assert derive_state(monitor, good, None)[0] == "healthy"
    assert derive_state(monitor, bad, None)[0] == "unhealthy"
    assert derive_state(monitor, good, bad)[0] == "degraded"
    assert derive_state(monitor, {**good, "error": "response_too_large"}, None)[0] == "unhealthy"
    assert derive_state({**monitor, "expected_status": 503}, bad, None)[0] == "healthy"
    assert derive_state(monitor, {**good, "latency_ms": 3000}, None)[0] == "degraded"


async def test_history_and_evidence(settings, http_server):
    url, _ = http_server
    store = Storage(settings.data_dir, embedding_identity(settings))
    target = store.save_service(
        ServiceInput(
            name="Example API",
            kind="other",
            monitor={"url": url + "/ok", "health_url": url + "/fail"},
        )
    )
    try:
        empty = await live_answer(ChatRequest(message="Is Example API currently online?"), store)
        assert not empty["grounded"]
        assert not store.observations(target["id"])
        observation = await probe(store, target)
        assert observation["state"] == "degraded"
        for i in range(HISTORY_LIMIT + 3):
            store.record_observation({**observation, "id": str(i)})
        assert len(store.observations(target["id"])) == HISTORY_LIMIT
        answer = await live_answer(ChatRequest(message="Why is Example API unavailable?"), store)
        assert answer["quality"] == "live_observation"
        assert answer["live"][0].health_endpoint["status_code"] == 503
        assert "LIVE OBSERVATION" in answer["answer"]
        assert "INDEXED DOCUMENTATION" in answer["answer"]
        recent = await live_answer(ChatRequest(message="Has Example API failed recently?"), store)
        assert len(recent["live"][0].recent_observations) == 10
    finally:
        store.close()
    reopened = Storage(settings.data_dir, embedding_identity(settings))
    assert len(reopened.observations(target["id"])) == HISTORY_LIMIT
    reopened.delete_service(target["id"])
    assert reopened.observations(target["id"]) == []
    reopened.close()


def test_api(client, http_server, provider):
    url, requests = http_server
    target = client.post(
        "/api/services",
        json={"name": "Example API", "kind": "other", "monitor": {"url": url + "/ok"}},
    ).json()
    sid = target["id"]
    assert client.get(f"/api/services/{sid}/health").json()["state"] == "unknown"
    assert not requests
    assert client.post(f"/api/services/{sid}/probe").json()["state"] == "healthy"
    assert (
        client.get(f"/api/services/{sid}/health").json()["latest"]["endpoint"]["status_code"] == 200
    )
    assert len(client.post("/api/services/probe").json()) == 1
    before = len(requests)
    for stream in (False, True):
        response = client.post(
            "/api/chat", json={"message": "Is Example API currently online?", "stream": stream}
        )
        assert response.status_code == 200
        assert "live_observation" in response.text
    assert len(requests) == before  # Reads/chat never probe implicitly.
    assert not provider.calls and not provider.embedding_calls
    assert (
        client.post("/api/services/00000000-0000-0000-0000-000000000000/probe").status_code == 404
    )
    inventory = client.post("/api/services", json={"name": "Inventory", "kind": "other"}).json()
    assert client.post(f"/api/services/{inventory['id']}/probe").status_code == 422
    assert (
        client.post(
            "/api/services", json={"name": "Bad", "kind": "other", "monitor": {"url": "file:///a"}}
        ).status_code
        == 422
    )


def test_cli(settings, http_server, monkeypatch, capsys, tmp_path):
    url, requests = http_server
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    config = tmp_path / "target.json"
    config.write_text(
        json.dumps({"name": "Self", "kind": "other", "monitor": {"url": url + "/ok"}})
    )
    assert cli.main(["services", "add", str(config), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["services", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["services"][0]["name"] == "Self"
    assert not requests
    assert cli.main(["services", "probe", "Self", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["service_results"][0]["state"] == "healthy"
    assert cli.main(["services", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["service_results"][0]["latest"]
    assert cli.main(["chat", "Is Self currently online?", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["quality"] == "live_observation"
    assert cli.main(["services", "probe", "missing", "--json"]) == 1


def test_exact_name_preferred_and_live_never_uses_documentation():
    services = [{"name": "Grimoire local"}, {"name": "Grimoire readiness"}]
    assert plan_live("Is Grimoire local online?", services).services == services[:1]
    assert plan_live("Is Missing online?", []).services == []
    assert plan_live("What port does Grimoire local use?", services) is None


async def test_invalid_runtime_url_does_not_echo_secrets():
    for url in ("http://[", "http://user:secret@localhost/", "file:///secret"):
        result = await probe_endpoint(url)
        assert result["error_kind"] == "invalid_url"
        assert "secret" not in json.dumps(result)


async def test_tls_failure_and_sni(monkeypatch):
    import ipaddress

    from app.services import probe as module

    async def resolve(*args):
        return [ipaddress.ip_address("127.0.0.1")]

    monkeypatch.setattr(module, "resolve", resolve)

    def failure(request):
        assert request.extensions["sni_hostname"] == "homelab.example"
        assert request.headers["host"] == "homelab.example"
        raise httpx.ConnectError("unsafe remote text") from ssl.SSLCertVerificationError()

    result = await probe_endpoint(
        "https://homelab.example/", transport=httpx.MockTransport(failure)
    )
    assert result["error_kind"] == "tls_error"
    assert "unsafe remote text" not in json.dumps(result)
    with pytest.raises(ValueError):
        ServiceInput(
            name="TLS",
            kind="other",
            monitor={
                "url": "https://localhost/",
                "verify_tls": False,
            },
        )


async def test_dns_deadline(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(2)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", slow)
    start = time.monotonic()
    result = await probe_endpoint("http://slow.example/", 0.1)
    assert result["error_kind"] == "timeout"
    assert time.monotonic() - start < 1


def test_explicit_chat_probe_and_interpretation(client, http_server):
    url, requests = http_server
    client.post(
        "/api/services",
        json={
            "name": "Local API",
            "kind": "other",
            "monitor": {"url": url + "/fail"},
        },
    )
    answer = client.post(
        "/api/chat",
        json={
            "message": "Is Local API online?",
            "probe": True,
            "explain": True,
        },
    ).json()
    assert len(requests) == 1
    assert answer["live"][0]["status_code"] == 503
    assert answer["live"][0]["state"] == "unhealthy"
    assert answer["live"][0]["probed_now"] is True
    assert "503" in answer["answer"]
    assert answer["interpretation"]


async def test_global_retention_and_backup(settings, http_server, tmp_path):
    from app.services.backup import create_backup, restore_backup

    url, _ = http_server
    settings = settings.model_copy(update={"data_dir": tmp_path / "state"})
    store = Storage(settings.data_dir, embedding_identity(settings))
    target = store.save_service(ServiceInput(name="Local", kind="other", monitor={"url": url}))
    observation = await probe(store, target)
    with store.connect() as db:
        for index in range(101):
            sid = f"s{index}"
            db.execute("INSERT INTO services VALUES (?,?,?)", (sid, sid, "{}"))
            db.executemany(
                "INSERT INTO service_observations VALUES (?,?,?,?)",
                [
                    (f"{sid}-{i}", sid, observation["checked_at"], json.dumps(observation))
                    for i in range(50)
                ],
            )
    store.record_observation({**observation, "id": "latest"})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM service_observations").fetchone()[0] == 5000
    store.close()
    backup = create_backup(settings.data_dir, tmp_path / "snapshot")
    restore_backup(Path(backup["backup"]), tmp_path / "restored")
    reopened = Storage(tmp_path / "restored", embedding_identity(settings))
    assert reopened.observations(target["id"])[0]["id"] == "latest"
    reopened.close()
