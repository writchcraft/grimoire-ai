"""Application-rendered transport evidence and timestamped independent layers."""

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool

from app.models import LiveEvidence
from app.services import host, tcp


def mapped_records(storage, service):
    mapping = service.get("local_windows_service")
    if not mapping:
        return []
    # Historical snapshots only; never assert these describe the current service state.
    for page in host.history(storage.path.parent, 50):
        records = [
            r
            for r in page["records"]
            if r["facts"].get("name", "").casefold() == mapping.casefold()
        ]
        if records:
            return records
    return []


def same_target(item, other):
    if other.evidence_type == "http_observation" and other.endpoint:
        parts = urlsplit(other.endpoint)
        return (parts.hostname or "").rstrip(".").casefold() == item["hostname"].rstrip(
            "."
        ).casefold() and (parts.port or (443 if parts.scheme == "https" else 80)) == item["port"]
    if other.evidence_type == "tls_observation" and other.tls:
        return other.hostname == item["hostname"] and other.tls.get("port") == item["port"]
    return False


async def append_tcp(body, plan, storage, answer, evidence):
    if plan.kind == "tls" and not re.search(r"\b(tcp|ports?|reach|why)\b", body.message, re.I):
        return answer, evidence
    if not re.search(
        r"\b(tcp|ports?|reach|refus\w*|why|fail\w*|unreachable|unavailable|"
        r"changed|stopped|associated)\b",
        body.message,
        re.I,
    ):
        return answer, evidence
    services = plan.services
    if sum(len(tcp.targets(s)) for s in services) > tcp.MAX_BATCH:
        return answer + "\nTCP batch exceeds 50 endpoints; select a service.", evidence
    if body.probe:
        await tcp.inspect_many(storage, services)
    lines = [answer, "", "TCP OBSERVATIONS (single pinned address per target)"]
    for service in services:
        data = await run_in_threadpool(tcp.summary, storage, service)
        if not data["latest"]:
            lines.append(f"{service['name']}: TCP UNKNOWN; no configured target observation.")
        if body.probe and service.get("local_windows_service"):
            page = await run_in_threadpool(
                host.inspect_host,
                "services",
                host.ServiceQuery(name=service["local_windows_service"]),
                True,
            )
            await run_in_threadpool(host.retain, storage.path.parent, page)
        mapped = await run_in_threadpool(mapped_records, storage, service)
        if not data["latest"]:
            for record in mapped:
                lines.append(
                    f"[{record['evidence_id']}] Explicitly mapped Windows service "
                    f"{service['local_windows_service']}: "
                    f"{record['facts'].get('state', 'unknown')} at {record['observed_at']}; "
                    "retained snapshot, current state unconfirmed."
                )
        for item in data["latest"]:
            label = f"C{1 + sum(e.evidence_type == 'tcp_observation' for e in evidence)}"
            prior = [e for e in evidence if e.service == service["name"] and e.observation_id]
            evidence.append(
                LiveEvidence(
                    label=label,
                    evidence_type="tcp_observation",
                    tcp=item,
                    observation_id=item["id"],
                    service=service["name"],
                    hostname=item["hostname"],
                    state=item["state"],
                    observed_at=item["observed_at"],
                    latency_ms=item["latency_ms"],
                    reason=item["reason"],
                    error=item["error_category"],
                    recent_observations=[
                        o
                        for o in data["history"]
                        if (o["hostname"], o["port"]) == (item["hostname"], item["port"])
                    ][:10],
                )
            )
            stale = datetime.now(UTC) - datetime.fromisoformat(item["observed_at"])
            lines.append(
                f"[{label}] {service['name']}: TCP :{item['port']} "
                f"{item['state'].upper()}; host {item['hostname']}; "
                f"address {item['address']}; connection {item['connection_latency_ms']} ms; "
                f"observed {item['observed_at']}. {item['reason']}"
                + (" STALE; inspect again." if stale.total_seconds() > 300 else "")
            )
            for other in prior:
                lines.append(
                    f"[{label}][{other.label}] Separate observations: TCP "
                    f"{item['state']} at {item['observed_at']}; {other.evidence_type} "
                    f"{other.state} at {other.observed_at}. "
                    "Addresses and times may differ; causation unconfirmed."
                )
                if (
                    same_target(item, other)
                    and other.evidence_type == "http_observation"
                    and item["state"] == "reachable"
                ):
                    if other.status_code and other.status_code >= 500:
                        lines.append(
                            "TCP connection succeeded; HTTP returned a server error "
                            "in its separate application observation."
                        )
                    elif other.state == "healthy":
                        lines.append("TCP and HTTP checks succeeded at their respective times.")
            layers = []
            for kind, title in (
                ("dns_observation", "DNS"),
                ("tcp_observation", "TCP"),
                ("tls_observation", "TLS"),
                ("http_observation", "HTTP"),
            ):
                candidate = next(
                    (
                        e
                        for e in prior
                        if e.evidence_type == kind
                        and (
                            same_target(item, e)
                            or (kind == "dns_observation" and e.hostname == item["hostname"])
                        )
                    ),
                    None,
                )
                if kind == "tcp_observation":
                    layers.append(f"TCP :{item['port']} {item['state']} at {item['observed_at']}")
                else:
                    layers.append(
                        f"{title} {candidate.state} at {candidate.observed_at}"
                        if candidate
                        else f"{title} not observed for this target"
                    )
            lines.append(
                f"[{label}] Diagnostic layers (independent samples): " + " -> ".join(layers)
            )
            tls_failure = next(
                (
                    e
                    for e in prior
                    if same_target(item, e)
                    and e.evidence_type == "tls_observation"
                    and e.state in {"hostname-mismatch", "expired", "not-yet-valid", "untrusted"}
                ),
                None,
            )
            if item["state"] == "reachable" and tls_failure:
                lines.append(
                    f"[{label}][{tls_failure.label}] TCP established a connection; "
                    f"TLS reports {tls_failure.state} at {tls_failure.observed_at}. "
                    "TLS is an observed failing layer in that separate sample."
                )
            for record in mapped:
                lines.append(
                    f"[{label}][{record['evidence_id']}] Explicitly mapped Windows "
                    f"service {service['local_windows_service']}: "
                    f"{record['facts'].get('state', 'unknown')} at "
                    f"{record['observed_at']}. Retained host snapshot; current state "
                    "and causation unconfirmed."
                )
    if services and re.search(r"\b(changed|events?|around)\b", body.message, re.I):
        from datetime import timedelta

        from app.services.windows_events import EventQuery, read_events

        selected = await run_in_threadpool(tcp.summary, storage, services[0])
        if selected["latest"]:
            stamp = datetime.fromisoformat(selected["latest"][0]["observed_at"])
            page = await run_in_threadpool(
                read_events,
                EventQuery(
                    channel="System",
                    level="all",
                    start=stamp - timedelta(minutes=5),
                    end=stamp + timedelta(minutes=5),
                    limit=20,
                ),
            )
            lines.append(
                f"Local System events: {page['status']}; bounded to 20 records "
                "within five minutes of the first selected TCP observation. "
                "Time proximity only; endpoint host relationship and causation unconfirmed."
            )
            for record in page["records"]:
                label = f"W{1 + sum(e.evidence_type == 'windows_event' for e in evidence)}"
                evidence.append(
                    LiveEvidence(
                        label=label,
                        evidence_type="windows_event",
                        windows_event=record,
                        observation_id=record["evidence_id"],
                        service=services[0]["name"],
                        observed_at=record["timestamp"],
                        state=record["severity"],
                        reason="Nearby local System event; causation unconfirmed",
                    )
                )
                lines.append(
                    f"[{label}] {record['timestamp']}: {record['provider']} "
                    f"event {record['event_id']}; {record['message'] or 'unavailable'}"
                )
    lines.append(
        "Unobserved layers remain unavailable. TCP success proves connection "
        "establishment only. Timeout does not prove firewall blockage; refusal does "
        "not prove a crash. Retained manual samples are not uptime percentages."
    )
    return "\n".join(lines), evidence
