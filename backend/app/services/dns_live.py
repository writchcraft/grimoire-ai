"""Deterministic DNS evidence and careful point-in-time HTTP comparison."""

import re
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool

from app.models import LiveEvidence
from app.services.dns import MAX_BATCH, inspect_many, summary


async def append_dns(body, plan, storage, answer, evidence):
    if plan.kind == "tls" and not re.search(r"\b(dns|resolv\w*)\b", body.message, re.I):
        return answer, evidence
    dns_question = bool(re.search(r"\b(dns|resolv\w*|hostname|same ip)\b", body.message, re.I))
    correlation = bool(re.search(r"\b(why|unreachable|unavailable)\b", body.message, re.I))
    if not (dns_question or correlation):
        return answer, evidence
    targets = plan.services[:MAX_BATCH]
    if body.probe:
        await inspect_many(storage, targets)
    lines = [answer, "", "DNS OBSERVATIONS (system resolver; separate from HTTP)"]
    if len(plan.services) > MAX_BATCH:
        lines.append(f"Showing only the first {MAX_BATCH} configured services.")
    if not targets:
        lines.append(
            "No matching service is configured; only inventory hostnames can be inspected."
        )
    for service in targets:
        data = await run_in_threadpool(summary, storage, service)
        latest = data["latest"]
        label = f"D{len([e for e in evidence if e.evidence_type == 'dns_observation']) + 1}"
        if latest is None:
            lines.append(
                f"{service['name']}: DNS UNKNOWN; no DNS observation recorded. Inspect DNS first."
            )
            continue
        item = LiveEvidence(
            label=label,
            evidence_type="dns_observation",
            observation_id=latest["id"],
            service=service["name"],
            state=latest["status"],
            hostname=latest["hostname"],
            addresses=latest["addresses"],
            canonical_name=latest["canonical_name"],
            record_types=latest["record_types"] if latest["applicable"] else [],
            records=latest["records"],
            observed_at=latest["observed_at"],
            latency_ms=latest["latency_ms"],
            error=latest["error_category"],
            reason=latest["reason"],
            addresses_changed=data["addresses_changed"],
            recent_observations=data["history"],
        )
        evidence.append(item)
        stale = (datetime.now(UTC) - datetime.fromisoformat(item.observed_at)).total_seconds() > 300
        lines.append(
            f"[{label}] {item.service}: DNS {item.state.upper()}; {item.hostname}; "
            f"A: {', '.join(latest['a']) or 'none'}; AAAA: {', '.join(latest['aaaa']) or 'none'}; "
            f"canonical name: {item.canonical_name or 'not reported'}; "
            f"observed {item.observed_at}; "
            f"{item.latency_ms} ms. {item.reason}" + (" Stale; inspect again." if stale else "")
        )
        if item.addresses_changed is not None:
            previous = data["history"][1]
            lines.append(
                f"[{label}] Address set {'changed' if item.addresses_changed else 'unchanged'} "
                f"between {previous['observed_at']} ({', '.join(previous['addresses'])}) and "
                f"{item.observed_at} ({', '.join(item.addresses)})."
            )
        http = next(
            (
                e
                for e in evidence
                if e.service == item.service and e.evidence_type == "http_observation"
            ),
            None,
        )
        if http and http.observation_id:
            lines.append(
                f"[{label}][{http.label}] Separate observations: DNS {item.state} at "
                f"{item.observed_at}; HTTP {http.state} at {http.observed_at}. "
                "These checks may differ in time, cache and resolver result; "
                "they do not establish causation."
            )
    lines.append(
        "Only recorded samples are compared; no claim of long-term DNS stability. "
        "DNS success does not establish HTTP reachability."
    )
    return "\n".join(lines), evidence
