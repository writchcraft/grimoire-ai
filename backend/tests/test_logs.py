from app.services.log_analysis import inspect_logs

LOGS = """2026-10-28T12:00:00Z INFO nginx started
2026-10-28T12:00:02Z ERROR upstream connection refused pid=123
2026-10-28T12:00:03Z ERROR upstream connection refused pid=456
2026-10-28T12:00:04Z WARNING retrying upstream
192.0.2.1 - - [28/Oct/2026:12:00:05 +0000] "GET / HTTP/1.1" 502 173
"""


def test_log_severity_repetition_and_evidence_lines():
    result = inspect_logs(LOGS)
    assert result["line_count"] == 5
    assert result["error_count"] == 3 and result["warning_count"] == 1
    assert result["events"][0]["line"] == 2
    assert result["repeated_patterns"][0]["count"] == 2
    assert result["time_range"]["first_observed"] == "2026-10-28T12:00:00Z"


def test_event_cap_preserves_total_counts():
    result = inspect_logs("ERROR failed\n" * 100)
    assert result["error_count"] == 100
    assert len(result["events"]) == 80 and result["events_truncated"]


def test_log_analysis_works_without_model(client, provider):
    response = client.post("/api/logs/analyse", json={"content": LOGS, "service": "nginx"})
    assert response.status_code == 200
    assert response.json()["explanation"] is None
    assert not provider.calls


def test_incident_export_includes_notes_events_and_uncertainty(client):
    response = client.post(
        "/api/incidents/summarise",
        json={
            "content": LOGS,
            "service": "nginx",
            "title": "Proxy incident",
            "notes": "Checked dependency health.\n```\nmalicious fence",
        },
    )
    report = response.json()["markdown"]
    assert "# Proxy incident" in report
    assert "L2 [error]" in report and "Checked dependency health" in report
    assert "hypothesis" in report and "No commands" in report
    assert "````text" in report


def test_model_log_explanation_is_evidence_bounded(client, provider):
    response = client.post("/api/logs/analyse", json={"content": LOGS, "use_model": True})
    assert response.status_code == 200 and response.json()["explanation"]
    assert "uncertainty" in provider.calls[0][0]["content"]
    assert '"line": 2' in provider.calls[0][1]["content"]
