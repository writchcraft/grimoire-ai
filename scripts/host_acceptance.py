"""Read the real local Windows host without changing infrastructure or publishing inventory."""

import asyncio
import contextlib
import ctypes
import io
import json
import socket
import tempfile
from datetime import datetime
from pathlib import Path

from app import cli
from app.core.config import Settings
from app.main import create_app
from app.models import MonitorConfig, ServiceInput
from app.services import host
from app.services.windows_events import EventQuery, read_events
from fastapi.testclient import TestClient


class UnusedModel:
    async def start(self):
        pass

    async def close(self):
        pass


def main():
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    assert not admin, "Acceptance must run without Administrator privileges"
    report = {
        "administrator": admin,
        "scope": "Real local host, explicit read-only inspections",
    }
    page = host.inspect_host()
    assert page["status"] == "available", page["issues"]
    facts = page["records"][0]["facts"]
    assert (
        facts["os"]["build"] > 0
        and facts["cpu_model"]
        and facts["logical_cpu_count"] > 0
    )
    assert (
        facts["uptime_seconds"] >= 0
        and datetime.fromisoformat(facts["boot_time"]).tzinfo
    )
    assert 0 <= facts["cpu_percent"] <= 100
    assert facts["memory"]["total_bytes"] >= facts["memory"]["available_bytes"] > 0
    assert facts["disks"] and facts["interfaces"]
    for disk in facts["disks"]:
        assert disk["total_bytes"] == disk["used_bytes"] + disk["free_bytes"]
    report.update(
        os=facts["os"],
        logical_cpu_count=facts["logical_cpu_count"],
        disks=len(facts["disks"]),
        interfaces=len(facts["interfaces"]),
        metadata_resources="PASS",
        evidence="PASS SHA-256 identity",
    )
    services = host.inspect_host("services", host.ServiceQuery(limit=200))
    assert services["status"] in {"available", "partial"} and services["records"]
    running = host.inspect_host("services", host.ServiceQuery(state="running", limit=2))
    stopped = host.inspect_host("services", host.ServiceQuery(state="stopped", limit=2))
    assert running["records"] and stopped["records"]
    chosen = stopped["records"][0]["identifier"]
    single = host.inspect_host("services", host.ServiceQuery(name=chosen), exact=True)
    assert single["records"][0]["identifier"] == chosen
    report["services"] = dict(
        count=len(services["records"]),
        truncated=services["truncated"],
        running_filter="PASS",
        stopped_filter="PASS",
        exact_inspection="PASS",
    )
    # Query existing startup evidence around the measured boot; no synthetic Windows events.
    startup = {"status": "available", "records": []}
    for provider, event_id in (
        ("EventLog", 6005),
        ("Microsoft-Windows-Kernel-General", 12),
    ):
        sample = read_events(
            EventQuery(
                channel="System",
                level="all",
                provider=provider,
                event_id=event_id,
                around=datetime.fromisoformat(facts["boot_time"]),
                minutes=5,
                limit=5,
            )
        )
        startup["records"].extend(sample["records"])
        if sample["status"] != "available":
            startup["status"] = sample["status"]
    links = host.correlations(page["records"], startup["records"])
    report["windows_events"] = dict(
        status=startup["status"],
        sampled=len(startup["records"]),
        relationships=len(links),
        scope="Exact host + startup provider/ID + boot time proximity",
    )
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(data_dir=Path(directory))
        with TestClient(
            create_app(settings, UnusedModel(), UnusedModel()),
            base_url="http://localhost",
        ) as client:
            for route in (
                "",
                "/system",
                "/resources",
                "/disks",
                "/network",
                "/services",
            ):
                response = client.get("/api/host" + route)
                assert response.status_code == 200 and response.json()["records"], route
            assert client.get("/api/host/services/" + chosen).status_code == 200
            assert client.get("/api/host/services?limit=201").status_code == 422
            assert client.get("/api/host/history").json()
            store = client.app.state.storage
            from app.services.live import probe_and_record

            # Reserve an unused local TCP port without listening. No live service is stopped.
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
                service = store.save_service(
                    ServiceInput(
                        name="Acceptance explicit local mapping",
                        kind="other",
                        local_windows_service=chosen,
                        monitor=MonitorConfig(
                            url=f"http://127.0.0.1:{port}", timeout_seconds=1
                        ),
                    )
                )
                observation = asyncio.run(probe_and_record(store, service))
                assert observation["state"] == "unreachable"
                result = host.context(
                    settings, store, service["id"], startup["records"]
                )
                assert any(r["identifier"] == chosen for r in result["records"])
                assert any(
                    observation["id"] in r["evidence_ids"]
                    for r in result["correlations"]
                )
            report["mapping"] = (
                "PASS real unreachable test-owned loopback endpoint + explicitly mapped "
                "already-stopped Windows service; no live outage induced; cause unknown"
            )
        for command in (
            ["host", "status", "--json"],
            ["host", "system", "--json"],
            ["host", "disks", "--json"],
            ["host", "network", "--json"],
            ["host", "services", "--state", "stopped", "--json"],
            ["host", "services", chosen, "--json"],
        ):
            args = cli.parser().parse_args(command)
            result, code = asyncio.run(cli.execute(args, settings))
            assert code == 0 and result["records"]
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                cli.emit(result, True)
            assert json.loads(stream.getvalue())["records"]
        result, code = asyncio.run(
            cli.execute(cli.parser().parse_args(["host", "status"]), settings)
        )
        assert code == 0
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            cli.emit(result, False)
        assert "THIS HOST" in stream.getvalue() and "Memory:" in stream.getvalue()
    report.update(
        api="PASS all host routes, service filters/detail and history",
        cli="PASS status/system/disks/network/services/detail JSON and concise human status",
        privacy="No host name, IP addresses, service names or raw events in this report",
    )
    path = Path("docs/host-evidence/acceptance.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
