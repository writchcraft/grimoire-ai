"""Deterministic Windows event fixtures; no dependence on host event IDs."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import cli
from app.services import windows_events as ev


def fixture(
    level=2,
    provider="Example",
    event_id=7031,
    record=10,
    channel="System",
    stamp="2026-10-29T19:42:14Z",
):
    xml = f'''<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>
    <Provider Name="{provider}"/><EventID>{event_id}</EventID><Level>{level}</Level>
    <TimeCreated SystemTime="{stamp}"/><EventRecordID>{record}</EventRecordID>
    <Channel>{channel}</Channel><Computer>fixture-host</Computer><Task>0</Task></System>
    <EventData><Data Name="Service">Example service</Data></EventData></Event>'''
    return ev.parse_event(xml, "Exact fixture message")


@pytest.mark.parametrize(
    "level,name",
    [(1, "critical"), (2, "error"), (3, "warning"), (4, "info"), (5, "verbose"), (0, "unknown")],
)
def test_native_levels(level, name):
    r = fixture(level)
    assert r["severity"] == name
    assert r["message"] == "Exact fixture message"
    assert r["record_id"] == 10
    assert r["timestamp"] == "2026-10-29T19:42:14+00:00"
    assert r["event_data"] == [{"name": "Service", "value": "Example service"}]
    assert r["evidence_id"] == fixture(level)["evidence_id"]


def test_missing_and_malformed():
    r = ev.parse_event(
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System/></Event>'
    )
    assert r["message"] is None and r["event_id"] is None and r["severity"] == "unknown"
    for bad in ("<Event/>", "<!DOCTYPE x><Event/>", "x" * 65537):
        with pytest.raises(ValueError):
            ev.parse_event(bad)


def test_grouping_does_not_merge_providers_or_channels():
    data = ev.assemble(
        [fixture(record=11), fixture(), fixture(provider="Other"), fixture(channel="Application")]
    )
    assert len(data["repeated_patterns"]) == 1
    group = data["repeated_patterns"][0]
    assert group["provider"] == "Example" and group["count"] == 2
    assert len(group["timestamps"]) == 2
    assert data["events"][0]["source"].startswith("WE-")
    assert data["error_count"] == 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 201},
        {"limit": 0},
        {"provider": "x' or Level=1"},
        {"channel": "Other"},
        {"event_id": 65536},
        {"start": "2026-10-29T12:00:00"},
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-29T00:00:00Z"},
        {"start": "2026-10-30T00:00:00Z", "end": "2026-10-29T00:00:00Z"},
        {"minutes": 61},
    ],
)
def test_bad_queries(kwargs):
    with pytest.raises(ValueError):
        ev.EventQuery(**kwargs)


def test_xpath_bounds_and_time():
    q = ev.EventQuery(
        around=datetime(2026, 10, 29, 19, 42, tzinfo=UTC),
        event_id=7031,
        provider="Example",
        level="error",
        before_record=100,
    )
    x = ev.xpath(q)
    assert "Level <= 2" in x and "EventID=7031" in x
    assert "19:37:00.000000Z" in x and "19:47:00.000000Z" in x
    assert "EventRecordID < 100" in x and "Provider[@Name='Example']" in x


@pytest.mark.parametrize(
    "code,reason",
    [
        (5, "permission denied"),
        (15007, "channel unavailable"),
        (1, "Windows event API unavailable"),
    ],
)
def test_unavailable(code, reason):
    class Native:
        def read(self, query):
            raise OSError(code, "never expose this sensitive exception")

    r = ev.read_events(ev.EventQuery(), Native())
    assert r["status"] == "unavailable" and r["reason"] == reason
    assert "sensitive" not in json.dumps(r)


def test_platform_and_limit(monkeypatch):
    monkeypatch.setattr(ev.sys, "platform", "linux")
    assert ev.read_events(ev.EventQuery())["reason"] == "not available on this platform"

    class Native:
        def read(self, query):
            return [fixture(record=n + 1) for n in range(12)], []

    assert len(ev.read_events(ev.EventQuery(limit=2), Native())["records"]) == 2


def test_api_validation(client, monkeypatch):
    calls = []

    def read(q):
        calls.append(q)
        return {"records": [fixture()], "status": "available", "issues": []}

    monkeypatch.setattr(ev, "read_events", read)
    assert client.get("/api/events?limit=201").status_code == 422
    assert client.get("/api/events?channel=Other").status_code == 422
    assert client.get("/api/events?start=bad").status_code == 422
    assert client.get("/api/events?level=error&event_id=7031&limit=2").status_code == 200
    assert calls[-1].event_id == 7031 and calls[-1].limit == 2
    assert client.get("/api/events/10?channel=Application").status_code == 200
    assert calls[-1].channel == "Application" and calls[-1].record_id == 10
    assert client.get("/api/events/channels").status_code == 200


def test_selected_incident_and_identity(client, monkeypatch):
    r = fixture()
    monkeypatch.setattr(
        ev, "read_events", lambda q: {"records": [r], "status": "available", "issues": []}
    )
    ref = {k: r[k] for k in ("channel", "record_id", "evidence_id")}
    body = {"selected": [ref], "incident_title": "Fixture incident", "notes": "Operator report"}
    response = client.post("/api/events/analyse", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert r["evidence_id"] in saved["markdown"]
    assert saved["analysis"]["events"][0]["evidence"] == r
    assert "Confirmed" in saved["markdown"] and "Unknown" in saved["markdown"]
    mixed = client.post(
        "/api/incidents/summarise",
        json={"content": "2026-10-29T19:42:00Z error api unavailable", "event_selection": [ref]},
    )
    assert mixed.status_code == 200, mixed.text
    assert r["evidence_id"] in mixed.json()["markdown"]
    ref["evidence_id"] = "WE-" + "0" * 64
    assert client.post("/api/events/analyse", json=body).status_code == 409


async def test_model_privacy_and_rejection(provider, monkeypatch):
    from app.services.grounding import model_events

    records = [fixture(channel="Security"), fixture()]
    data = ev.assemble(records)
    assert len(model_events(data)) == 1
    assert "Exact fixture message" in json.dumps(model_events(data))
    assert "source_xml" not in json.dumps(model_events(data))
    result = await ev.analyse_events(records, "Explain", provider, True)
    assert result["quality"] == "local_model_required" and not provider.calls
    monkeypatch.setattr(ev, "local_model_allowed", lambda _: True)
    provider.chat = lambda _: None

    async def malicious(messages):
        return '{"interpretations":[{"kind":"unknown","supporting_lines":[999]}]}'

    provider.chat = malicious
    result = await ev.analyse_events(records, "Explain", provider, True)
    assert result["quality"] == "rejected_output"
    assert result["events"][0]["evidence"] == records[0]


def test_model_local_route():
    for address, allowed in [
        ("http://127.0.0.1:11434", False),
        ("https://example.org", False),
        ("http://192.0.2.1", False),
    ]:
        provider = SimpleNamespace(
            settings=SimpleNamespace(ai_provider="ollama", ollama_url=address)
        )
        assert ev.local_model_allowed(provider) == allowed


def test_correlation():
    class Store:
        def service(self, name):
            return {"id": "fixture-service"}

        def observations(self, *_):
            return [
                {"id": "HTTP-1", "checked_at": "2026-10-29T19:43:00Z", "state": "unreachable"},
                {"id": "HTTP-2", "checked_at": "2026-10-29T18:00:00Z"},
            ]

        def dns_observations(self, *_):
            return []

        def tls_observations(self, *_):
            return []

    result = ev.correlate([fixture()], Store(), "example")
    assert len(result) == 1 and result[0]["observation"]["id"] == "HTTP-1"
    assert "causation unconfirmed" in result[0]["relationship"]
    assert result[0]["event_evidence_ids"] == [fixture()["evidence_id"]]


def test_cli_namespace():
    args = cli.parser().parse_args(
        ["events", "search", "--event-id", "7031", "--channel", "System", "--limit", "2", "--json"]
    )
    assert args.command == "events" and args.event_id == 7031 and args.limit == 2
    assert cli.parser().parse_args(["events", "inspect", "10"]).record_id == 10


def test_host_question_chat_and_stream(client, monkeypatch):
    records = [fixture()]
    calls = []

    def read(query):
        calls.append(query)
        return {"records": records, "status": "available", "issues": []}

    monkeypatch.setattr(ev, "read_events", read)
    response = client.post(
        "/api/chat", json={"message": "What errors happened on this PC recently?"}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["live"][0]["evidence_type"] == "windows_event"
    assert records[0]["evidence_id"] in result["answer"]
    assert [q.channel for q in calls] == ["System", "Application"]
    assert all(q.start and q.end and q.limit == 20 for q in calls)
    stream = client.post(
        "/api/chat",
        json={"message": "Summarise critical events from the last hour.", "stream": True},
    )
    assert stream.status_code == 200 and '"type": "done"' in stream.text
    assert calls[-1].level == "critical"
    assert (calls[-1].end - calls[-1].start).total_seconds() == 3600


async def test_security_model_input_excluded(provider, monkeypatch):
    monkeypatch.setattr(ev, "local_model_allowed", lambda _: True)
    result = await ev.analyse_events(
        [fixture(channel="Security"), fixture()], "Explain", provider, True
    )
    payload = json.loads(provider.calls[0][1]["content"])
    assert len(payload["events"]) == 1
    assert "Security" not in json.dumps(payload)
    assert "source_xml" not in json.dumps(payload)
    assert len(result["events"]) == 2


def test_native_only_local_model():
    provider = SimpleNamespace(settings=SimpleNamespace(ai_provider="native"))
    assert ev.local_model_allowed(provider)


def test_selection_deduplicates_and_bounds(monkeypatch):
    from fastapi import HTTPException

    from app.api.events import selection
    from app.models import EventReference

    record = fixture()
    ref = EventReference(**{k: record[k] for k in ("channel", "record_id", "evidence_id")})
    monkeypatch.setattr(ev, "read_events", lambda _: {"records": [record]})
    assert len(selection([ref, ref])) == 1
    record["message"] = "x" * 1_048_576
    with pytest.raises(HTTPException) as error:
        selection([ref])
    assert error.value.status_code == 413


async def test_mixed_timeline_keeps_line_refs_and_orders_times(provider):
    from app.services.log_analysis import analyse

    extra = ev.assemble([fixture(stamp="2026-10-29T19:42:14Z")])
    result = await analyse(
        "2026-10-29T20:00:00Z ERROR late\nERROR unknown time",
        "fixture",
        "Explain",
        False,
        provider,
        extra_evidence=extra,
    )
    assert [e["line"] for e in result["events"]] == [3, 1, 2]
    assert result["time_range"]["first_observed"] == "2026-10-29T19:42:14+00:00"
    assert result["events"][0]["evidence"]["source_timestamp"] == "2026-10-29T19:42:14Z"
