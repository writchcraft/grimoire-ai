import asyncio
import json
import socket
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app import cli
from app.models import ChatRequest, ServiceInput
from app.services import dns
from app.services.live import live_answer
from app.services.runtime import embedding_identity
from app.services.storage import Storage


def info(address, canonical=""):
    return (
        socket.AF_INET6 if ":" in address else socket.AF_INET,
        socket.SOCK_STREAM,
        6,
        canonical,
        (address, 0),
    )


async def lookup(host):
    assert host == "grafana.home.arpa"
    return [info("192.168.1.20"), info("::1", "target.home.arpa"), info("192.168.1.21")]


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["::1"], ["::1", "192.168.1.20", "192.168.1.20"]]
)
async def test_records(addresses):
    async def resolve(host):
        return [info(a) for a in addresses]

    item = await dns.inspect_hostname("test.home.arpa", resolve)
    assert item["status"] == "resolved"
    assert set(item["addresses"]) == set(addresses)
    assert set(item["a"] + item["aaaa"]) == set(addresses)
    assert item["observed_at"] and item["latency_ms"] >= 0


async def test_canonical_and_bounds():
    item = await dns.inspect_hostname("grafana.home.arpa", lookup)
    assert item["canonical_name"] == "target.home.arpa"

    async def large(host):
        return [info(f"10.0.0.{i}") for i in range(1, 100)]

    item = await dns.inspect_hostname("test", large)
    assert item["status"] == "partial" and item["truncated"]
    assert len(item["addresses"]) == dns.MAX_RESULTS


@pytest.mark.parametrize(
    "host",
    ["", "bad host", "x..test", "-bad", "a" * 64, "a/evil", "bad\nname", "é.test", "a" * 254],
)
async def test_invalid(host):
    async def never(host):
        pytest.fail("Invalid hostname reached resolver")

    assert (await dns.inspect_hostname(host, never))["status"] == "invalid"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", None])
async def test_not_applicable(host):
    async def never(host):
        pytest.fail("Literal IP reached resolver")

    item = await dns.inspect_hostname(host, never)
    assert not item["applicable"] and not item["addresses"]
    assert item["status"] == "unknown"


@pytest.mark.parametrize(
    "code,state", [(socket.EAI_NONAME, "unresolved"), (socket.EAI_AGAIN, "unknown")]
)
async def test_failure(code, state):
    async def fail(host):
        raise socket.gaierror(code, "untrusted resolver message")

    item = await dns.inspect_hostname("test", fail)
    assert item["status"] == state
    assert "untrusted" not in json.dumps(item)


async def test_timeout_and_empty():
    async def slow(host):
        await asyncio.sleep(1)

    assert (await dns.inspect_hostname("test", slow, timeout=0.001))["status"] == "timeout"

    async def empty(host):
        return []

    assert (await dns.inspect_hostname("test", empty))["status"] == "unresolved"


async def test_worker_slots_survive_timeout(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def blocking(*args):
        calls.append(args)
        entered.set()
        release.wait(3)
        return [info("127.0.0.1")]

    monkeypatch.setattr(dns, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(socket, "getaddrinfo", blocking)
    try:
        assert (await dns.inspect_hostname("test", timeout=0.05))["status"] == "timeout"
        assert entered.is_set()
        busy = await dns.inspect_hostname("test", timeout=0.05)
        assert busy["error_category"] == "busy"
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.sleep(0.05)


def target(store):
    return store.save_service(
        ServiceInput(
            name="Grafana", kind="other", monitor={"url": "https://grafana.home.arpa:3000"}
        )
    )


async def test_inventory_history_evidence(settings, monkeypatch):
    store = Storage(settings.data_dir, embedding_identity(settings))
    service = target(store)
    monkeypatch.setattr(dns, "system_lookup", lookup)
    item = await dns.inspect_and_record(store, service)
    assert dns.hostname_for(service) == "grafana.home.arpa"
    assert (await dns.inspect_and_record(store, service))["id"] == item["id"]
    old = {
        **item,
        "id": "old",
        "observed_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
    }
    old["checked_at"] = old["observed_at"]
    old["addresses"] = ["192.168.1.19"]
    store.record_dns(old)
    assert dns.summary(store, service)["addresses_changed"] is True
    answer = await live_answer(ChatRequest(message="What does Grafana resolve to?"), store)
    evidence = next(e for e in answer["live"] if e.evidence_type == "dns_observation")
    assert evidence.observation_id == item["id"]
    assert evidence.addresses_changed is True
    assert "192.168.1.19" in answer["answer"]
    assert "long-term" in answer["answer"]
    assert answer["grounded"]
    # DNS-only questions do not require a speculative HTTP request, even on refresh.
    refreshed = await live_answer(
        ChatRequest(message="What does Grafana resolve to?", probe=True), store
    )
    assert all(e.evidence_type == "dns_observation" for e in refreshed["live"])
    assert not store.observations(service["id"])
    for i in range(55):
        store.record_dns({**item, "id": str(i)})
    assert len(store.dns_observations(service["id"])) == 50
    store.close()
    store = Storage(settings.data_dir, embedding_identity(settings))
    assert len(store.dns_observations(service["id"])) == 50
    store.delete_service(service["id"])
    assert not store.dns_observations(service["id"])
    store.close()


async def test_comparison_and_batch(settings):
    item = await dns.inspect_hostname("grafana.home.arpa", lookup)
    assert dns.address_change(item, item) is False
    assert dns.address_change({**item, "status": "partial"}, item) is None
    assert dns.address_change({**item, "hostname": "other"}, item) is None
    with pytest.raises(ValueError, match="50"):
        await dns.inspect_many(None, [{"monitor": {"url": "http://localhost"}}] * 51)


async def test_absolute_query_and_alias_no_followups():
    queries = []

    async def alias(host):
        queries.append(host)
        return [info("127.0.0.1", "alias.home.arpa")]

    item = await dns.inspect_hostname("TEST.home.arpa.", alias)
    assert queries == ["test.home.arpa."]
    assert item["queried_hostname"] == "test.home.arpa."
    assert item["canonical_name"] == "alias.home.arpa"
    assert item["records"] == [{"type": "A", "value": "127.0.0.1"}]


async def test_correlation_and_explicit_refresh(settings, monkeypatch):
    store = Storage(settings.data_dir, embedding_identity(settings))
    service = target(store)
    monkeypatch.setattr(dns, "system_lookup", lookup)
    # Save a measured HTTP failure; DNS refresh must not change or overwrite it.
    http = dict(
        id="http-failure",
        service_id=service["id"],
        service=service["name"],
        checked_at=datetime.now(UTC).isoformat(),
        state="unreachable",
        reason="Connection timed out.",
        endpoint=dict(
            url=service["monitor"]["url"], status_code=None, latency_ms=100, error="timeout"
        ),
    )
    store.record_observation(http)
    missing = await live_answer(ChatRequest(message="Why is Grafana unreachable?"), store)
    assert "no DNS observation recorded" in missing["answer"]
    # Mock HTTP here: refreshing DNS never supplies HTTP's security resolver or transport.
    from app.services import live

    async def recorded(*args):
        return {service["id"]: http}

    monkeypatch.setattr(live, "probe_many", recorded)
    from app.services import tls

    async def tls_failure(*args, **kwargs):
        return dict(
            hostname="grafana.home.arpa",
            port=3000,
            state="handshake-failed",
            observed_at=datetime.now(UTC).isoformat(),
            latency_ms=1,
            reason="TCP/TLS connection failed.",
        )

    monkeypatch.setattr(tls, "inspect_tls", tls_failure)
    answer = await live_answer(
        ChatRequest(message="Why is Grafana unreachable?", probe=True), store
    )
    assert "do not establish causation" in answer["answer"]
    assert len(answer["live"]) == 3
    assert answer["live"][2].evidence_type == "tls_observation"
    assert answer["live"][2].state == "handshake-failed"
    assert answer["live"][0].state == "unreachable"
    assert answer["live"][1].state == "resolved"
    assert store.observations(service["id"])[0] == http
    assert len(store.dns_observations(service["id"])) == 1
    store.close()


async def test_global_history_bound(settings):
    store = Storage(settings.data_dir, embedding_identity(settings))
    service = target(store)
    item = await dns.inspect_and_record(store, service, lookup)
    services = [store.save_service(ServiceInput(name=f"seed{i}", kind="other")) for i in range(101)]
    with store.connect() as db:
        db.executemany(
            "INSERT INTO dns_observations VALUES (?,?,?,?)",
            [
                (f"seed-{s['id']}-{i}", s["id"], item["checked_at"], json.dumps(item))
                for s in services
                for i in range(50)
            ],
        )
    store.record_dns({**item, "id": "last"})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM dns_observations").fetchone()[0] == 5000
    store.close()


def test_api(client, monkeypatch):
    monkeypatch.setattr(dns, "system_lookup", lookup)
    service = client.post(
        "/api/services",
        json={
            "name": "Grafana",
            "kind": "other",
            "monitor": {"url": "https://grafana.home.arpa:3000"},
        },
    ).json()
    path = f"/api/services/{service['id']}/dns"
    assert client.get(path).json()["latest"] is None
    item = client.post(path).json()
    assert item["status"] == "resolved"
    assert client.get(path).json()["latest"]["id"] == item["id"]
    assert client.get("/api/services?health=true").json()[0]["dns"]["latest"]["id"] == item["id"]
    response = client.post(
        "/api/chat", json={"message": "Does grafana.home.arpa currently resolve?"}
    ).json()
    assert response["grounded"]
    assert any(e["evidence_type"] == "dns_observation" for e in response["live"])
    assert client.post("/api/services/00000000-0000-0000-0000-000000000000/dns").status_code == 404


def test_inventory_hostname_without_http_monitor(client, monkeypatch):
    monkeypatch.setattr(dns, "system_lookup", lookup)
    service = client.post(
        "/api/services",
        json={
            "name": "Grafana",
            "kind": "other",
            "hostname": "grafana.home.arpa",
        },
    ).json()
    assert dns.hostname_for(service) == "grafana.home.arpa"
    result = client.post(f"/api/services/{service['id']}/dns").json()
    assert result["status"] == "resolved"
    # The monitored target wins when an inventory hostname differs.
    assert dns.hostname_for({**service, "monitor": {"url": "http://127.0.0.1"}}) == "127.0.0.1"


async def test_cli(settings, monkeypatch, capsys):
    monkeypatch.setattr(dns, "system_lookup", lookup)
    with_store = Storage(settings.data_dir, embedding_identity(settings))
    target(with_store)
    with_store.close()
    for args in (["services", "dns", "Grafana", "--json"], ["services", "dns"]):
        parsed = cli.parser().parse_args(args)
        result, code = await cli.execute(parsed, settings)
        assert code == 0 and result["dns_results"][0]["status"] == "resolved"
        cli.emit(result, parsed.json)
        text = capsys.readouterr().out
        if parsed.json:
            assert json.loads(text)["dns_results"][0]["hostname"] == "grafana.home.arpa"
        else:
            assert "Status: RESOLVED" in text and "AAAA: ::1" in text
