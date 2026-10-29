import copy
import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from app import cli
from app.models import ServiceInput
from app.services import host


@pytest.fixture
def native(monkeypatch):
    import psutil

    p = host.WindowsHostProvider()
    monkeypatch.setattr(host.platform, "win32_ver", lambda: ("11", "10.0", "", ""))
    monkeypatch.setattr(host.platform, "win32_edition", lambda: "Professional")
    monkeypatch.setattr(host.sys, "getwindowsversion", lambda: NS(build=26100), raising=False)
    monkeypatch.setattr(p, "cpu_model", lambda: "Example CPU")
    monkeypatch.setattr(psutil, "boot_time", lambda: 1000)
    monkeypatch.setattr(host.time, "time", lambda: 4600)
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval: 25 if interval == 0.1 else None)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 8)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: NS(total=1000, available=100, percent=90))
    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda all: [
            NS(device="C:\\", mountpoint="C:\\", fstype="NTFS", opts="rw,fixed"),
            NS(device="Z:\\", mountpoint="Z:\\", fstype="NTFS", opts="rw,remote"),
        ],
    )
    monkeypatch.setattr(
        psutil, "disk_usage", lambda _: NS(total=1000, free=10, used=990, percent=99)
    )
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Ethernet": NS(isup=True)})
    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: {
            "Ethernet": [NS(family=socket.AF_INET, address="192.0.2.1")],
            "Loopback": [NS(family=socket.AF_INET6, address="::1")],
        },
    )
    return p


def test_os_uptime_cpu_memory_disks_interfaces(native):
    page = host.inspect_host(provider=native)
    f = page["records"][0]["facts"]
    assert f["os"]["build"] == 26100 and f["os"]["edition"] == "Professional"
    assert f["uptime_seconds"] == 3600 and f["boot_time"].startswith("1970-")
    assert f["cpu_percent"] == 25 and f["logical_cpu_count"] == 8
    assert f["memory"] == dict(
        total_bytes=1000, available_bytes=100, used_bytes=900, used_percent=90, state="high"
    )
    assert len(f["disks"]) == 1 and f["disks"][0]["space_state"] == "low"
    assert f["interfaces"][0]["addresses"] == ["192.0.2.1"]
    assert f["interfaces"][1]["loopback"] and f["interfaces"][1]["state"] is None
    record = page["records"][0]
    assert (
        host.evidence(
            record["evidence_type"], record["host"], record["observed_at"], record["identifier"], f
        )
        == record
    )
    assert "binpath" not in json.dumps(page)


def test_inaccessible_fields_continue(native, monkeypatch):
    def denied(*_):
        raise PermissionError()

    monkeypatch.setattr(native.api, "disk_usage", denied)
    monkeypatch.setattr(native.api, "virtual_memory", denied)
    monkeypatch.setattr(native, "cpu_model", denied)
    page = host.inspect_host(provider=native)
    facts = page["records"][0]["facts"]
    assert page["status"] == "partial"
    assert facts["memory"] is None and facts["cpu_model"] is None
    assert facts["os"]["name"] == "Windows"
    assert facts["disks"][0]["space_state"] == "unavailable"
    host.assessment(page["records"])


class Service:
    def __init__(self, name, state, start="manual", denied=False):
        self.n, self.s, self.start, self.denied = name, state, start, denied

    def name(self):
        return self.n

    def display_name(self):
        return self.n + " Display"

    def status(self):
        return self.s

    def start_type(self):
        if self.denied:
            raise PermissionError()
        return self.start

    def pid(self):
        return 123 if self.s == "running" else None


@pytest.fixture
def services(native, monkeypatch):
    items = [
        Service("Alpha", "running", "automatic"),
        Service("Beta", "stopped"),
        Service("Gamma", "paused", denied=True),
    ]
    monkeypatch.setattr(native.api, "win_service_iter", lambda: iter(items), raising=False)
    monkeypatch.setattr(
        native.api,
        "win_service_get",
        lambda name: next(s for s in items if s.name().lower() == name.lower()),
        raising=False,
    )
    return native


@pytest.mark.parametrize(
    "filters,names",
    [
        ({"state": "running"}, ["Alpha"]),
        ({"state": "stopped"}, ["Beta"]),
        ({"start_type": "automatic"}, ["Alpha"]),
        ({"name": "ET"}, ["Beta"]),
        ({"state": "running", "start_type": "manual"}, []),
    ],
)
def test_service_filters(services, filters, names):
    page = host.inspect_host("services", host.ServiceQuery(**filters), provider=services)
    assert [r["identifier"] for r in page["records"]] == names


def test_service_bounds_exact_and_partial(services):
    page = host.inspect_host("services", host.ServiceQuery(limit=1), provider=services)
    assert len(page["records"]) == 1 and page["truncated"]
    page = host.inspect_host("services", host.ServiceQuery(name="Beta"), True, services)
    assert page["records"][0]["facts"]["state"] == "stopped"
    page = host.inspect_host("services", provider=services)
    assert page["status"] == "partial"
    assert page["records"][2]["facts"]["start_type"] is None


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 201},
        {"state": "restart"},
        {"start_type": "enable"},
        {"name": "x" * 257},
    ],
)
def test_invalid_filters(query):
    with pytest.raises(ValueError):
        host.ServiceQuery(**query)


def test_thresholds_assessment_and_identity(native):
    page = host.inspect_host(provider=native)
    result = host.assessment(page["records"])
    assert result["confirmed"] and result["probable"] and result["unknown"]
    record = page["records"][0]
    altered = copy.deepcopy(record["facts"])
    altered["cpu_percent"] = 0
    assert (
        host.evidence(
            record["evidence_type"],
            record["host"],
            record["observed_at"],
            record["identifier"],
            altered,
        )["evidence_id"]
        != record["evidence_id"]
    )
    with pytest.raises(ValueError):
        host.Thresholds(disk_used_percent=float("nan"))


def test_history_bound_local_only(native, tmp_path):
    for _ in range(55):
        host.retain(tmp_path, host.inspect_host("system", provider=native))
    assert len(host.history(tmp_path, 999)) == 50
    assert len(host.history(tmp_path, 2)) == 2


def test_unsupported_busy(monkeypatch):
    monkeypatch.setattr(host.sys, "platform", "linux")
    assert host.inspect_host()["status"] == "unavailable"
    assert host._slots.acquire(False) and host._slots.acquire(False)
    try:
        assert host.inspect_host(provider=NS())["issues"] == ["Host inspection busy; retry later"]
    finally:
        host._slots.release()
        host._slots.release()


def test_enumeration_denied_and_disappeared_service(services, monkeypatch):
    def denied():
        raise PermissionError()

    monkeypatch.setattr(services.api, "win_service_iter", denied)
    page = host.inspect_host("services", provider=services)
    assert page["status"] == "partial" and not page["records"] and page["issues"]

    def missing(name):
        raise services.api.NoSuchProcess(0, name)

    monkeypatch.setattr(services.api, "win_service_get", missing)
    page = host.inspect_host("services", host.ServiceQuery(name="Absent"), True, services)
    assert page["status"] == "available" and not page["records"]


def test_interface_bounds_report_partial(native, monkeypatch):
    monkeypatch.setattr(
        native.api,
        "net_if_addrs",
        lambda: {str(i): [NS(family=socket.AF_INET, address="192.0.2.1")] * 33 for i in range(129)},
    )
    result = host.inspect_host("network", provider=native)
    assert result["status"] == "partial"
    interfaces = result["records"][0]["facts"]["interfaces"]
    assert len(interfaces) == 128 and len(interfaces[0]["addresses"]) == 32


def test_exact_service_config_denied_keeps_accessible_state(services, monkeypatch):
    def denied(name):
        raise services.api.AccessDenied(0, name)

    monkeypatch.setattr(services.api, "win_service_get", denied)
    result = host.inspect_host("services", host.ServiceQuery(name="Gamma"), True, services)
    assert result["status"] == "partial"
    assert result["records"][0]["facts"]["state"] == "paused"
    assert result["records"][0]["facts"]["start_type"] is None


def test_correlation_strict_host_identity_time_and_explicit_mapping():
    now = datetime.now(UTC)
    record = host.evidence(
        "windows_service_observation",
        "host",
        now.isoformat(),
        "Alpha",
        dict(name="Alpha", display_name="Alpha Display", state="stopped"),
    )
    event = dict(
        computer="HOST",
        timestamp=now.isoformat(),
        provider="Service Control Manager",
        event_id=7031,
        evidence_id="WE-example",
        event_data=[dict(value="Alpha")],
    )
    assert len(host.correlations([record], [event])) == 1
    assert not host.correlations([record], [{**event, "computer": "other"}])
    assert not host.correlations([record], [{**event, "event_data": [dict(value="Alpha2")]}])
    assert not host.correlations(
        [record], [{**event, "timestamp": (now - timedelta(days=1)).isoformat()}]
    )
    http = dict(id="http-1", state="unreachable", checked_at=now.isoformat())
    assert not host.correlations([record], http=http)
    assert len(host.correlations([record], http=http, mapped_name="Alpha")) == 1
    assert not host.correlations([record], http=http, mapped_name="Alpha2")
    boot = host.evidence(
        "host_observation",
        "host",
        now.isoformat(),
        "status",
        dict(boot_time=now.isoformat(), uptime_seconds=4),
    )
    assert host.correlations([boot], [{**event, "provider": "EventLog", "event_id": 6005}])
    assert not host.correlations([boot], [{**event, "provider": "Unrelated", "event_id": 6005}])


def test_api_and_cli(client, native, monkeypatch, settings, capsys):
    monkeypatch.setattr(host, "WindowsHostProvider", lambda: native)
    monkeypatch.setattr(host.sys, "platform", "win32")
    for path in ("", "/system", "/resources", "/disks", "/network"):
        response = client.get("/api/host" + path)
        assert response.status_code == 200 and response.json()["records"]
    assert client.get("/api/host/history?limit=51").status_code == 422
    assert client.get("/api/host/services?limit=201").status_code == 422
    assert client.get("/api/host/services?state=restart").status_code == 422
    assert client.post("/api/host/services/Alpha/start").status_code in {404, 405}
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["host", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["records"][0]["evidence_type"] == "host_observation"
    assert cli.main(["host", "status"]) == 0
    assert "THIS HOST" in capsys.readouterr().out


def test_service_api_cli_and_incident_privacy(client, services, settings, monkeypatch, provider):
    monkeypatch.setattr(host, "WindowsHostProvider", lambda: services)
    monkeypatch.setattr(host.sys, "platform", "win32")
    page = client.get("/api/host/services?state=stopped").json()
    assert page["records"][0]["identifier"] == "Beta"
    assert client.get("/api/host/services/Beta").json()["records"][0]["facts"]["state"] == "stopped"
    from app.services.storage import Storage

    store = Storage(settings.data_dir, "test")
    try:
        configured = store.save_service(
            ServiceInput(name="API", kind="other", local_windows_service="Beta")
        )
        result = host.context(settings, store, configured["id"])
        assert any(r["identifier"] == "Beta" for r in result["records"])
    finally:
        store.close()
    response = client.post(
        "/api/incidents/summarise",
        json=dict(content="ERROR Application failed", include_host=True, use_model=True),
    )
    assert response.status_code == 200
    assert "host_context" in response.json()["analysis"]
    assert all("host_observation" not in json.dumps(call) for call in provider.calls)
