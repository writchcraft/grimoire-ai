import json
import re
from collections import Counter

from app.ai.base import ModelProvider
from app.services.grounding import explain_logs, model_events

TIMESTAMP = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?|"
    r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
)
ERROR = re.compile(
    r"\b(error|fatal|panic|critical|failed|failure|denied|refused|timeout|timed out)\b", re.I
)
WARNING = re.compile(r"\b(warn(?:ing)?|retry(?:ing)?|degraded|unhealthy)\b", re.I)
HTTP_ERROR = re.compile(r'"\s+([45]\d\d)\s')


def inspect_logs(content: str) -> dict:
    lines = content.splitlines()
    counts: Counter = Counter()
    events, times = [], []
    errors = warnings = 0
    for number, line in enumerate(lines, 1):
        timestamp = TIMESTAMP.match(line)
        if timestamp:
            times.append(timestamp.group())
        status = HTTP_ERROR.search(line)
        severity = (
            "error"
            if ERROR.search(line) or (status and status[1].startswith("5"))
            else ("warning" if WARNING.search(line) or status else "info")
        )
        errors += severity == "error"
        warnings += severity == "warning"
        if severity != "info":
            message = TIMESTAMP.sub("", line).strip()
            # Group named counters, preserving distinct error codes, ports and addresses.
            fingerprint = re.sub(r"\b(pid|attempt|count)[= ]+\d+\b", r"\1=#", message)
            counts[fingerprint] += 1
            if len(events) < 80:
                events.append(
                    {
                        "line": number,
                        "timestamp": timestamp.group() if timestamp else None,
                        "severity": severity,
                        "text": line,
                        "source": "supplied_logs",
                    }
                )
    return {
        "line_count": len(lines),
        "error_count": errors,
        "warning_count": warnings,
        "time_range": {
            "first_observed": times[0] if times else None,
            "last_observed": times[-1] if times else None,
        },
        "events": events,
        "events_truncated": errors + warnings > len(events),
        "repeated_patterns": [
            {"pattern": pattern[:500], "count": count}
            for pattern, count in counts.most_common(10)
            if count > 1
        ],
        "limitations": "Heuristic severity and repetition detection; not anomaly detection. "
        "Time range follows input order. Timestamps are not timezone-normalized.",
    }


async def analyse(
    content: str,
    service: str,
    question: str,
    use_model: bool,
    provider: ModelProvider,
    notes: str = "",
    extra_evidence: dict | None = None,
) -> dict:
    evidence = inspect_logs(content)
    if extra_evidence:
        offset = evidence["line_count"]
        evidence["events"].extend(
            {**event, "line": event["line"] + offset} for event in extra_evidence["events"]
        )
        evidence["line_count"] += extra_evidence["line_count"]
        evidence["error_count"] += extra_evidence["error_count"]
        evidence["warning_count"] += extra_evidence["warning_count"]
        evidence["repeated_patterns"].extend(extra_evidence["repeated_patterns"])
        evidence["correlations"] = extra_evidence.get("correlations", [])
        evidence["windows_events"] = True
        from app.services.windows_events import instant

        def event_order(event):
            try:
                return (0, instant(event["timestamp"]).isoformat(), event["line"])
            except (TypeError, ValueError, AttributeError):
                return (1, "", event["line"])

        evidence["events"].sort(key=event_order)
        known = [e for e in evidence["events"] if event_order(e)[0] == 0]
        evidence["time_range"] = {
            "first_observed": known[0]["timestamp"] if known else None,
            "last_observed": known[-1]["timestamp"] if known else None,
        }
        evidence["limitations"] += (
            " Mixed timeline sorts explicit timezone timestamps; unknown/naive times follow "
            "in source order and are not assigned an invented timezone."
        )
    explanation = None
    interpretation, quality = None, "not_requested"
    if use_model and extra_evidence:
        from app.services.windows_events import local_model_allowed

        if not local_model_allowed(provider):
            use_model = False
            quality = "local_model_required"
            explanation = "Event interpretation requires an entirely local model route."
    if use_model:
        interpretation, explanation, quality = await explain_logs(
            provider, evidence, question, notes
        )
    return {
        "service": service,
        **evidence,
        "explanation": explanation,
        "interpretation": interpretation,
        "quality": quality,
        "model_event_lines": [e["line"] for e in model_events(evidence)] if use_model else [],
        "suggested_investigation": [
            "Inspect dependency health and configuration without changing state.",
            "Compare these events with resource metrics and recent change records.",
        ],
    }


def incident_markdown(
    title: str, service: str, notes: str, evidence: dict, explanation: str | None
) -> str:
    # Fence user data with a delimiter longer than any embedded backtick sequence.
    def fence(text: str) -> str:
        longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
        delimiter = "`" * max(3, longest + 1)
        return f"{delimiter}text\n{text}\n{delimiter}"

    period = evidence["time_range"]
    native = any(
        e.get("evidence", {}).get("evidence_type") == "windows_event" for e in evidence["events"]
    )
    qualification = (
        (
            "Native event levels for Windows records; text counts for supplied logs. "
            "Windows timestamps normalized to UTC; supplied log times retain their source format."
        )
        if native
        else "Counts are heuristic."
    )
    correlations = evidence.get("correlations", [])

    timeline = (
        "\n".join(
            f"L{event['line']} [{event['severity']}] "
            + (f"[{event['source']}] " if event.get("evidence") else "")
            + event["text"]
            for event in evidence["events"]
        )
        or "No error/warning patterns detected in supplied lines."
    )
    return (
        f"# {title.replace(chr(10), ' ')}\n\n"
        f"## Affected service\n\n{fence(service)}\n\n"
        f"## Observed window\n\n{period['first_observed'] or 'Unknown'} → "
        f"{period['last_observed'] or 'Unknown'} "
        + (
            "(Windows times UTC; other explicit offsets retained; naive times unknown)"
            if native
            else "(input order; timezone not normalized)"
        )
        + "\n\n"
        f"## Confirmed\n\n{evidence['line_count']} lines; {evidence['error_count']} error matches; "
        f"{evidence['warning_count']} warning matches. {qualification}\n\n"
        f"## Relevant events / timeline\n\n{fence(timeline)}\n\n"
        f"{'Event list capped at 80 entries. ' if evidence['events_truncated'] else ''}"
        f"\n## Separate time-correlated observations\n\n"
        f"{fence(json.dumps(correlations, indent=2))}\n\n"
        f"\n## Operator notes / reported troubleshooting\n\n{fence(notes or 'None supplied.')}\n\n"
        f"## Probable\n\n"
        f"{
            explanation
            or 'Undetermined from heuristic analysis. Correlate the cited lines with '
            'service configuration, dependency health, resource usage and recent changes.'
        }\n\n"
        "## Unknown\n\nRoot cause and successful recovery cannot be established from these "
        "heuristics. Operator notes are unverified reports.\n\n"
        "## Suggested investigation\n\nInspect dependency health, configuration, resource usage "
        "and recent changes using read-only checks.\n\n"
        "## Confidence / uncertainty\n\nCounts describe matching text, not verified service state. "
        "Any proposed cause is a hypothesis. No commands or remediation were executed.\n"
    )
