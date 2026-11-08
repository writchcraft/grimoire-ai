import asyncio
import errno
import json
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app import cli
from app.models import ChatRequest, MonitorConfig, ServiceInput
from app.services import host, tcp
from app.services.live import live_answer, plan_live
from app.services.runtime import embedding_identity
from app.services.storage import Storage
from app.services.windows_events import correlate


@asynccontextmanager
async def listener(address="127.0.0.1", http=False):
    class Received(list):
        done = asyncio.Event()

    received = Received()

    async def handle(reader, writer):
        data = await reader.read(1000)
        received.append(data)
        received.done.set()
        if http and data:
            writer.write(b"HTTP/1.0 500 Error\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, address, 0)
    try:
        yield server.sockets[0].getsockname()[1], received
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
async def test_real_connection_and_refusal(address):
    try:
        async with listener(address) as (port, received):
            item = await tcp.inspect_tcp(address, port)
            assert item["state"] == "reachable" and item["connection_success"]
            assert item["address"] == address and item["connection_latency_ms"] >= 0
            await asyncio.wait_for(received.done.wait(), 1)
            assert received == [b""]  # No application bytes, even a banner request.
    except OSError:
        if address == "::1":
            pytest.skip("IPv6 loopback unavailable")
        raise
    assert (await tcp.inspect_tcp(address, port))["state"] == "refused"


@pytest.mark.parametrize(
    "host_name,port",
    [
        ("bad host", 80),
        ("127.0.0.1/24", 80),
        ("host1-host9:80", 80),
        ("127.0.0.1", 0),
        ("127.0.0.1", 65536),
        ("127.0.0.1", True),
        ("127.0.0.1", "80-90"),
        ("::1%lo", 80),
        (None, 80),
        ("https://a", 80),
    ],
)
async def test_invalid_single_targets(host_name, port):
    assert (await tcp.inspect_tcp(host_name, port))["state"] == "invalid"


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "0.0.0.0", "224.0.0.1", "::", "ff02::1", "::ffff:169.254.169.254"],
)
async def test_policy_blocks_before_connect(address, monkeypatch):
    async def forbidden(*args):
        pytest.fail("Blocked target was connected")

    monkeypatch.setattr(tcp, "connect", forbidden)
    assert (await tcp.inspect_tcp(address, 80))["error_category"] == "blocked_target"


async def test_resolution_pinning_and_bounds(monkeypatch):
    called = []

    async def lookup(name):
        assert name == "local.test."
        return [
            (socket.AF_INET, 1, 6, "", ("127.0.0.2", 0)),
            (socket.AF_INET, 1, 6, "", ("127.0.0.1", 0)),
        ]

    async def connect(address, port):
        called.append((address, port))

    monkeypatch.setattr(tcp, "system_lookup", lookup)
    monkeypatch.setattr(tcp, "connect", connect)
    item = await tcp.inspect_tcp("local.test.", 8080)
    assert item["state"] == "reachable" and called == [("127.0.0.1", 8080)]

    async def huge(name):
        return [(socket.AF_INET, 1, 6, "", ("127.0.0.1", 0))] * 33

    monkeypatch.setattr(tcp, "system_lookup", huge)
    assert (await tcp.inspect_tcp("local.test", 80))["error_category"] == "blocked_target"

    async def mixed(name):
        return [(socket.AF_INET, 1, 6, "", (ip, 0)) for ip in ["127.0.0.1", "169.254.169.254"]]

    monkeypatch.setattr(tcp, "system_lookup", mixed)
    assert (await tcp.inspect_tcp("local.test", 80))["error_category"] == "blocked_target"
    assert len(called) == 1


async def test_dns_failure_timeout_and_resolver_capacity(monkeypatch):
    async def missing(name):
        raise socket.gaierror(socket.EAI_NONAME, "untrusted remote text")

    monkeypatch.setattr(tcp, "system_lookup", missing)
    item = await tcp.inspect_tcp("missing.test", 80)
    assert item["state"] == "resolution-failed" and item["address"] is None
    assert "untrusted remote text" not in json.dumps(item)

    async def stalled(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr(tcp, "system_lookup", stalled)
    assert (await tcp.inspect_tcp("local.test", 80, timeout=0.01))["state"] == "timeout"
    monkeypatch.setattr(tcp, "connect", stalled)
    item = await tcp.inspect_tcp("127.0.0.1", 80, timeout=0.01)
    assert item["state"] == "timeout" and item["address"] == "127.0.0.1"
    assert "firewall" not in item["reason"]

    async def busy(name):
        raise BlockingIOError()

    monkeypatch.setattr(tcp, "system_lookup", busy)
    assert (await tcp.inspect_tcp("local.test", 80))["error_category"] == "busy"


@pytest.mark.parametrize(
    "code,state,category",
    [
        (errno.ECONNREFUSED, "refused", "connection_refused"),
        (errno.EHOSTUNREACH, "blocked/unreachable", "host_unreachable"),
        (errno.ENETUNREACH, "blocked/unreachable", "host_unreachable"),
        (errno.EACCES, "blocked/unreachable", "access_denied"),
        (errno.ETIMEDOUT, "timeout", "timeout"),
        (errno.EIO, "unknown", "connection_failed"),
    ],
)
async def test_error_semantics(monkeypatch, code, state, category):
    async def fails(*args):
        raise OSError(code, "untrusted")

    monkeypatch.setattr(tcp, "connect", fails)
    item = await tcp.inspect_tcp("127.0.0.1", 80)
    assert (item["state"], item["error_category"]) == (state, category)
    assert "untrusted" not in item["reason"]


async def test_concurrency_and_cancellation_release(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def stalled(*args):
        nonlocal count
        count += 1
        if count == 4:
            entered.set()
        await release.wait()

    monkeypatch.setattr(tcp, "connect", stalled)
    tasks = [asyncio.create_task(tcp.inspect_tcp("127.0.0.1", 80)) for _ in range(4)]
    await asyncio.wait_for(entered.wait(), 1)
    assert (await tcp.inspect_tcp("127.0.0.1", 80))["error_category"] == "busy"
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    release.set()
    assert (await tcp.inspect_tcp("127.0.0.1", 80))["state"] == "reachable"


def configured(store, port=8080):
    return store.save_service(
        ServiceInput(
            name="Grafana",
            kind="monitoring",
            hostname="127.0.0.1",
            ports=[port],
            monitor=MonitorConfig(url=f"http://127.0.0.1:{port}"),
        )
    )


def test_inventory():
    service = dict(
        hostname="db.local",
        ports=[5432],
        urls=["https://web.local"],
        monitor=dict(url="http://web.local:8080", health_url="http://web.local:8080/health"),
    )
    assert tcp.targets(service) == [("web.local", 8080), ("web.local", 443), ("db.local", 5432)]
    assert tcp.targets(dict(ip_address="127.0.0.1", ports=[8080])) == [("127.0.0.1", 8080)]
    assert tcp.targets(dict(hostname="a", ports=[])) == []
    assert tcp.targets(dict(urls=["http://example.test"])) == [("example.test", 80)]


async def test_storage_cooldown_history_and_config_changes(settings, monkeypatch):
    store = Storage(settings.data_dir, embedding_identity(settings))
    try:
        service = configured(store)
        calls = 0

        async def connected(*args):
            nonlocal calls
            calls += 1

        monkeypatch.setattr(tcp, "connect", connected)
        a = await tcp.inspect_and_record(store, service)
        b = await tcp.inspect_and_record(store, service)
        assert a == b and calls == 1
        assert len(tcp.summary(store, service)["history"]) == 1
        changed = {**service, "monitor": None, "ports": [9090]}
        assert tcp.summary(store, changed)["latest"] == []
        assert (await tcp.inspect_and_record(store, changed))[0]["port"] == 9090
        for _ in range(60):
            store.record_tcp(
                {
                    **a[0],
                    "id": str(uuid4()),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            )
        assert len(store.tcp_observations(service["id"], 1000)) == 50
        with pytest.raises(ValueError, match="50"):
            await tcp.inspect_many(store, [service] * 51)
        store.delete_service(service["id"])
        assert store.tcp_observations(service["id"]) == []
    finally:
        store.close()


async def test_same_service_concurrent_requests(settings, monkeypatch):
    store = Storage(settings.data_dir, embedding_identity(settings))
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(tcp, "connect", stalled)
    service = configured(store)
    task = asyncio.create_task(tcp.inspect_and_record(store, service))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ValueError, match="in progress"):
            await tcp.inspect_and_record(store, service)
        release.set()
        await task
    finally:
        release.set()
        await task
        store.close()


def test_api_cli_json(client, settings, monkeypatch, capsys):
    service = client.post(
        "/api/services", json=dict(name="TCP", kind="other", hostname="127.0.0.1", ports=[1])
    ).json()
    path = f"/api/services/{service['id']}/tcp"
    assert client.get(path).json()["latest"] == []
    response = client.post(path)
    assert response.status_code == 200
    assert response.json()[0]["state"] in {"refused", "reachable"}
    data = client.get(path).json()
    assert data["latest"][0]["id"] == response.json()[0]["id"]
    assert client.get("/api/services?health=true").json()[0]["tcp"] == data
    assert client.post(f"/api/services/{uuid4()}/tcp").status_code == 404
    assert client.post(path, json={"host": "unrelated.test", "ports": [1, 2]}).status_code == 422
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["services", "tcp", "TCP", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tcp_results"][0]["port"] == 1
    assert cli.main(["services", "tcp", "TCP"]) == 0
    assert "Status:" in capsys.readouterr().out
    assert cli.main(["services", "tcp", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["tcp_results"]) == 1
    assert cli.main(["network", "tcp", "127.0.0.1", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tcp_results"][0]["address"] == "127.0.0.1"
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["network", "tcp", "127.0.0.1", "1-100"])


async def test_live_evidence_and_http_correlation(settings):
    store = Storage(settings.data_dir, embedding_identity(settings))
    try:
        async with listener(http=True) as (port, _):
            service = configured(store, port)
            response = await live_answer(
                ChatRequest(message="Why is Grafana unavailable?", probe=True), store
            )
            types = {e.evidence_type for e in response["live"]}
            assert {"tcp_observation", "http_observation", "dns_observation"} <= types
            assert "HTTP returned a server error" in response["answer"]
            item = next(e for e in response["live"] if e.evidence_type == "tcp_observation")
            assert item.tcp["connection_success"] and item.observation_id
            assert item.tcp["service_id"] == service["id"]
            assert "causation unconfirmed" in response["answer"]
        for question in [
            "Can Grimoire reach the Grafana port?",
            "Does DNS work but the port fail?",
            "Which configured services currently refuse connections?",
            "Is the associated Windows service stopped?",
            "What changed around the failure?",
        ]:
            assert plan_live(question, [service]) is not None
    finally:
        store.close()


async def test_tls_and_windows_correlations(settings):
    store = Storage(settings.data_dir, embedding_identity(settings))
    try:
        service = configured(store)
        service["local_windows_service"] = "Alpha"
        service["monitor"]["url"] = "https://127.0.0.1:8080"
        with store.connect() as db:
            db.execute(
                "UPDATE services SET data=? WHERE id=?",
                (json.dumps({k: v for k, v in service.items() if k != "id"}), service["id"]),
            )
        item = (await tcp.inspect_and_record(store, service))[0]
        now = datetime.now(UTC).isoformat()
        store.record_tls(
            dict(
                id="tls-1",
                service_id=service["id"],
                service=service["name"],
                hostname="127.0.0.1",
                port=8080,
                state="hostname-mismatch",
                observed_at=now,
                checked_at=now,
                latency_ms=1,
                reason="Hostname mismatch",
            )
        )
        record = host.evidence(
            "windows_service_observation", "host", now, "Alpha", dict(name="Alpha", state="stopped")
        )
        host.retain(settings.data_dir, dict(records=[record]))
        response = await live_answer(ChatRequest(message="Why is Grafana unavailable?"), store)
        assert {"tcp_observation", "tls_observation"} <= {e.evidence_type for e in response["live"]}
        assert "Explicitly mapped Windows service Alpha: stopped" in response["answer"]
        links = host.correlations([record], mapped_name="Alpha", tcp=[{**item, "state": "refused"}])
        assert links and item["id"] in links[0]["evidence_ids"]
        assert not host.correlations([record], tcp=[item])
        event = dict(evidence_id="event-1", timestamp=item["observed_at"])
        assert any(
            c["evidence_type"] == "tcp_observation"
            for c in correlate([event], store, service["id"])
        )
        old = {**event, "timestamp": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
        assert not correlate([old], store, service["id"])
    finally:
        store.close()


def test_global_retention_and_inventory_normalization(settings):
    store = Storage(settings.data_dir, embedding_identity(settings))
    try:
        service = configured(store)
        stamp = datetime.now(UTC).isoformat()
        row = dict(
            id="last",
            service_id=service["id"],
            checked_at=stamp,
            observed_at=stamp,
            hostname="127.0.0.1",
            port=8080,
        )
        seeds = [
            store.save_service(ServiceInput(name=f"TCP seed {i}", kind="other")) for i in range(101)
        ]
        with store.connect() as db:
            db.executemany(
                "INSERT INTO tcp_observations VALUES (?,?,?,?)",
                [
                    (f"{seed['id']}-{i}", seed["id"], stamp, json.dumps(row))
                    for seed in seeds
                    for i in range(50)
                ],
            )
        store.record_tcp(row)
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM tcp_observations").fetchone()[0] == 5000
        assert tcp.targets(
            dict(hostname="LOCALHOST", ports=[80], urls=["http://localhost/path?q=1"])
        ) == [("localhost", 80)]
        assert tcp.targets(dict(ports=[8080], monitor=dict(url="https://localhost"))) == [
            ("localhost", 443),
            ("localhost", 8080),
        ]
    finally:
        store.close()


async def test_expired_cooldown_and_changed_event_window(settings, monkeypatch, provider):
    from app.services import windows_events

    store = Storage(settings.data_dir, embedding_identity(settings))
    try:
        service = configured(store)

        async def connected(*args):
            pass

        monkeypatch.setattr(tcp, "connect", connected)
        first = (await tcp.inspect_and_record(store, service))[0]
        old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        with store.connect() as db:
            db.execute(
                "UPDATE tcp_observations SET record=?", (json.dumps({**first, "observed_at": old}),)
            )
        second = (await tcp.inspect_and_record(store, service))[0]
        assert second["id"] != first["id"]
        seen = []

        def read(query):
            seen.append(query)
            return dict(
                status="available",
                records=[
                    dict(
                        evidence_id="event-1",
                        timestamp=second["observed_at"],
                        severity="error",
                        provider="SCM",
                        event_id=7031,
                        message="Service terminated",
                    )
                ],
            )

        monkeypatch.setattr(windows_events, "read_events", read)
        result = await live_answer(
            ChatRequest(
                message="What changed around the failure?", service=service["id"], explain=True
            ),
            store,
            provider,
        )
        assert seen[0].limit == 20 and (seen[0].end - seen[0].start).total_seconds() == 600
        assert {"tcp_observation", "windows_event"} <= {e.evidence_type for e in result["live"]}
        assert "Time proximity only" in result["answer"]
        assert provider.calls and "Service terminated" not in json.dumps(provider.calls)
        assert plan_live("What is the Grafana TCP port?", [service]) is None
    finally:
        store.close()
