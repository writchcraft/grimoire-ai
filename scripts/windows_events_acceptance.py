"""Read existing Windows records only. Persist acceptance metadata, never event payloads."""

import asyncio
import contextlib
import ctypes
import io
import json
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import cli
from app.core.config import Settings
from app.main import create_app
from app.models import ServiceInput
from app.services import windows_events as ev
from app.services.live import probe_and_record
from fastapi.testclient import TestClient


def main():
    report = {
        "acceptance_process_administrator": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "channels": ev.channels()["channels"],
        "scope": "Existing real local Windows events; no events generated; no elevation or permission changes",
    }
    for name in ("System", "Application"):
        page = ev.read_events(ev.EventQuery(channel=name, level="all", limit=5))
        assert page["status"] == "available" and page["records"], name
        r = page["records"][0]
        filtered = ev.read_events(
            ev.EventQuery(
                channel=name,
                level="all",
                provider=r["provider"],
                event_id=r["event_id"],
                limit=3,
            )
        )
        assert filtered["records"] and all(
            e["event_id"] == r["event_id"] and e["provider"] == r["provider"]
            for e in filtered["records"]
        )
        window = ev.read_events(
            ev.EventQuery(
                channel=name, level="all", around=ev.instant(r["timestamp"]), limit=5
            )
        )
        assert window["records"], window
        inspected = ev.read_events(
            ev.EventQuery(channel=name, level="all", record_id=r["record_id"], limit=1)
        )
        assert inspected["records"][0]["evidence_id"] == r["evidence_id"]
        errors = ev.read_events(ev.EventQuery(channel=name, level="error", limit=3))
        assert all(e["level"] in (1, 2) for e in errors["records"])
        before = ev.read_events(
            ev.EventQuery(
                channel=name, level="all", before_record=r["record_id"], limit=3
            )
        )
        assert all(e["record_id"] < r["record_id"] for e in before["records"])
        report[name] = {
            "count": len(page["records"]),
            "rendered_messages": sum(
                e["message_status"] == "rendered" for e in page["records"]
            ),
            "error_sample_count": len(errors["records"]),
            "provider_event_filter": "PASS",
            "time_window": "PASS",
            "record_identity": "PASS",
            "pagination": "PASS",
        }
        if name == "System":
            selected = [
                {k: e[k] for k in ("channel", "record_id", "evidence_id")}
                for e in (errors["records"] or page["records"])[:3]
            ]
    for args in (
        ["events", "channels", "--json"],
        [
            "events",
            "recent",
            "--channel",
            "Application",
            "--level",
            "all",
            "--limit",
            "2",
            "--json",
        ],
        ["events", "inspect", str(selected[0]["record_id"]), "--json"],
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert cli.main(args) == 0
        assert json.loads(output.getvalue())
    report["cli"] = "PASS channels/recent/inspect"
    with tempfile.TemporaryDirectory(prefix="grimoire-events-") as directory:
        settings = Settings(data_dir=Path(directory))
        with TestClient(create_app(settings), base_url="http://localhost") as client:
            assert client.get("/api/events/channels").status_code == 200
            response = client.get("/api/events?channel=Application&level=all&limit=2")
            assert response.status_code == 200 and len(response.json()["records"]) == 2
            assert client.get("/api/events?limit=201").status_code == 422
            assert (
                client.get(
                    f"/api/events/{selected[0]['record_id']}?channel=System"
                ).status_code
                == 200
            )
            analysis = client.post(
                "/api/events/analyse",
                json={
                    "selected": selected,
                    "use_model": True,
                    "question": "Explain the observed Windows events without claiming a cause.",
                },
            )
            assert analysis.status_code == 200, analysis.text
            data = analysis.json()
            assert data["quality"] == "validated", data["quality"]
            assert all(e["source"] in data["answer"] for e in data["events"])
            incident = client.post(
                "/api/events/analyse",
                json={
                    "selected": selected,
                    "incident_title": "Real Windows acceptance",
                    "notes": "Existing bounded host sample; no induced failure.",
                },
            )
            assert incident.status_code == 200
            assert all(
                ref["evidence_id"] in incident.json()["markdown"] for ref in selected
            )

            class HealthyHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()

                def log_message(self, *_):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), HealthyHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                service = client.app.state.storage.save_service(
                    ServiceInput(
                        name="Local acceptance fixture",
                        kind="other",
                        monitor={"url": f"http://127.0.0.1:{server.server_port}/"},
                    )
                )
                observation = asyncio.run(
                    probe_and_record(client.app.state.storage, service)
                )
                assert observation["state"] == "healthy"
                stamp = ev.instant(observation["checked_at"])
                nearby = ev.read_events(
                    ev.EventQuery(
                        channel="System",
                        level="all",
                        start=stamp - timedelta(minutes=5),
                        end=stamp + timedelta(minutes=5),
                        limit=20,
                    )
                )
                relationships = ev.correlate(
                    nearby["records"], client.app.state.storage, service["id"]
                )
                assert all(
                    x["observation_id"] == observation["id"] for x in relationships
                )
                assert all(
                    "causation unconfirmed" in x["relationship"] for x in relationships
                )
                report["live_http_correlation"] = {
                    "http_state": observation["state"],
                    "nearby_real_events": len(nearby["records"]),
                    "relationships": len(relationships),
                    "result": "PASS actual HTTP and native timestamp comparison",
                    "scope": "Test-owned healthy loopback endpoint; no service outage induced",
                }
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            report["api"] = (
                "PASS channels/list/inspect/filters/bounds/selection/incident"
            )
            report["ai"] = {
                "quality": data["quality"],
                "selected_events": len(data["events"]),
                "interpretation": data["interpretation"],
                "source_references": "PASS",
            }
    report["correlation"] = (
        "Fixtures cover positive and excluded time matches; real healthy HTTP comparison above; no real service outage induced"
    )
    report["ui"] = "See browser acceptance result in windows-events-report.md"
    destination = Path("docs/windows-events-evidence/acceptance.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
