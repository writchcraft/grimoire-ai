import json
import re
from collections import Counter

from app.ai.base import ModelProvider
from app.rag.retrieval import SYSTEM

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
            # Group changing PIDs, durations and numeric counters; retain original evidence.
            fingerprint = re.sub(r"\b\d+\b", "#", message)
            counts[fingerprint] += 1
            if len(events) < 80:
                events.append(
                    {
                        "line": number,
                        "timestamp": timestamp.group() if timestamp else None,
                        "severity": severity,
                        "text": line[:1500],
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
) -> dict:
    evidence = inspect_logs(content)
    explanation = None
    if use_model:
        # Bounded evidence, including a sample of normal lines for context.
        material = {
            "service": service,
            "question": question,
            "observations": evidence,
            "sample": content[:3000],
            "operator_notes": notes,
        }
        # Bound before encoding to avoid breaking JSON or cutting off the question.
        material["observations"] = {**evidence, "events": evidence["events"][:12]}
        encoded = json.dumps(material, ensure_ascii=False)
        explanation = await provider.chat(
            [
                {
                    "role": "system",
                    "content": SYSTEM + "\nExplain observed symptoms in plain English. "
                    "Cite log line numbers. Separate evidence, probable causes, uncertainty "
                    "and suggested investigation. Never assert a cause as certain. "
                    "No evidence is proof of absence.",
                },
                {"role": "user", "content": "Untrusted log evidence (JSON):\n" + encoded},
            ]
        )
    return {"service": service, **evidence, "explanation": explanation}


def incident_markdown(
    title: str, service: str, notes: str, evidence: dict, explanation: str | None
) -> str:
    # Fence user data with a delimiter longer than any embedded backtick sequence.
    def fence(text: str) -> str:
        longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
        delimiter = "`" * max(3, longest + 1)
        return f"{delimiter}text\n{text}\n{delimiter}"

    period = evidence["time_range"]
    timeline = (
        "\n".join(
            f"L{event['line']} [{event['severity']}] {event['text']}"
            for event in evidence["events"]
        )
        or "No error/warning patterns detected in supplied lines."
    )
    return (
        f"# {title.replace(chr(10), ' ')}\n\n"
        f"## Affected service\n\n{fence(service)}\n\n"
        f"## Observed window\n\n{period['first_observed'] or 'Unknown'} → "
        f"{period['last_observed'] or 'Unknown'} (input order; timezone not normalized)\n\n"
        f"## Symptoms\n\n{evidence['line_count']} lines; {evidence['error_count']} error matches; "
        f"{evidence['warning_count']} warning matches. Counts are heuristic.\n\n"
        f"## Relevant events / timeline\n\n{fence(timeline)}\n\n"
        f"{'Event list capped at 80 entries. ' if evidence['events_truncated'] else ''}"
        f"\n## Operator notes / reported troubleshooting\n\n{fence(notes or 'None supplied.')}\n\n"
        f"## Probable cause and suggested investigation\n\n"
        f"{
            explanation
            or 'Undetermined from heuristic analysis. Correlate the cited lines with '
            'service configuration, dependency health, resource usage and recent changes.'
        }\n\n"
        "## Confidence / uncertainty\n\nCounts describe matching text, not verified service state. "
        "Any proposed cause is a hypothesis. No commands or remediation were executed.\n"
    )
