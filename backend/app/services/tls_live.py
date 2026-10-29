"""TLS facts are rendered by application code, with separate source identities."""

import json
import re
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool

from app.models import LiveEvidence
from app.services.tls import inspect_many, summary, target


async def append_tls(body, plan, storage, answer, evidence):
    if not re.search(
        r"\b(tls|ssl|certificates?|cert|expir\w*|why|fail\w*|unreachable|unavailable)\b",
        body.message,
        re.I,
    ):
        return answer, evidence
    targets = [s for s in plan.services if target(s)][:50]
    if body.probe:
        await inspect_many(storage, targets)
    lines = [answer, "", "TLS OBSERVATIONS (separate certificate, trust and hostname facts)"]
    if len(plan.services) > 50:
        lines.append("Only the first 50 HTTPS targets are shown.")
    if not targets:
        lines.append("No matching configured HTTPS service.")
    for service in targets:
        data = await run_in_threadpool(summary, storage, service)
        item = data["latest"]
        if not item:
            lines.append(f"{service['name']}: TLS UNKNOWN; no TLS observation recorded.")
            continue
        label = f"T{1 + sum(e.evidence_type == 'tls_observation' for e in evidence)}"
        evidence.append(
            LiveEvidence(
                label=label,
                evidence_type="tls_observation",
                observation_id=item["id"],
                service=service["name"],
                hostname=item["hostname"],
                state=item["state"],
                observed_at=item["observed_at"],
                latency_ms=item["latency_ms"],
                reason=item["reason"],
                error=item.get("error"),
                tls=item,
                recent_observations=data["history"],
            )
        )
        stale = (
            datetime.now(UTC) - datetime.fromisoformat(item["observed_at"])
        ).total_seconds() > 300
        lines.append(
            f"[{label}] {service['name']}: TLS {item['state'].upper()}; "
            + f"{item['hostname']}:{item['port']}; certificate presented: "
            f"{item.get('certificate_presented', 'unavailable')}; "
            f"subject: {item.get('subject', 'unavailable')}; "
            f"issuer: {item.get('issuer', 'unavailable')}; "
            f"valid from: {item.get('valid_from', 'unavailable')}; "
            f"expires: {item.get('expires_at', 'unavailable')}; "
            f"remaining at check: {item.get('remaining_days', 'unavailable')} days; "
            f"date validity: {item.get('validity', 'unknown')}; "
            f"hostname: {item.get('hostname_match', 'unavailable')}; "
            f"runtime trust: {item.get('trust', 'unavailable')} "
            f"({item.get('trust_reason') or 'no validation result'}); "
            f"protocol: {item.get('protocol') or 'unavailable'}; "
            f"observed: {item['observed_at']}; {item['latency_ms']} ms. {item['reason']}"
            + (" STALE: inspect again." if stale else "")
        )
        if data["changes"] is not None:
            previous = data["history"][1]
            lines.append(
                f"[{label}] Changes between retained observations "
                f"{previous['id']} at {previous['observed_at']} and {item['id']} "
                f"at {item['observed_at']}: {json.dumps(data['changes'])}. "
                "A changed certificate or later expiry alone does not prove renewal."
            )
        for other in evidence:
            if (
                other.service == service["name"]
                and other.evidence_type != "tls_observation"
                and other.observation_id
            ):
                lines.append(
                    f"[{label}][{other.label}] Separate observations: TLS {item['state']} "
                    f"at {item['observed_at']}; {other.evidence_type} {other.state} "
                    f"at {other.observed_at}; these do not establish causation."
                )
    lines.append(
        "Only retained samples support comparisons. No automatic HTTP verification bypass."
    )
    return "\n".join(lines), evidence
