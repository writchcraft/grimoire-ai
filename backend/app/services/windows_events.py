"""Read-only local Windows events. Only query, render, metadata and close APIs are bound."""

import ctypes
import hashlib
import json
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from app.models import InputModel

CHANNELS = ("System", "Application", "Security")
LEVELS = {"critical": 1, "error": 2, "warning": 3, "info": 4, "verbose": 5}
MAX_BYTES = 65536
_slots = threading.BoundedSemaphore(2)


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if dt.tzinfo is None:
        raise ValueError("Event times must include a timezone")
    return dt.astimezone(UTC)


class EventQuery(InputModel):
    channel: Literal["System", "Application", "Security"] = "System"
    level: Literal["critical", "error", "warning", "info", "verbose", "all"] = "warning"
    provider: str | None = Field(default=None, max_length=200, pattern=r"^[^'\"\[\]<>]*$")
    event_id: int | None = Field(default=None, ge=0, le=65535)
    record_id: int | None = Field(default=None, ge=1, le=2**63 - 1)
    before_record: int | None = Field(default=None, ge=1, le=2**63 - 1)
    start: datetime | None = None
    end: datetime | None = None
    around: datetime | None = None
    minutes: int = Field(default=5, ge=1, le=60)
    limit: int = Field(default=100, ge=1, le=200)

    @model_validator(mode="after")
    def window(self):
        for value in (self.start, self.end, self.around):
            if value is not None:
                instant(value)
        if self.around:
            if self.start or self.end:
                raise ValueError("Use around or start/end")
            self.start = self.around - timedelta(minutes=self.minutes)
            self.end = self.around + timedelta(minutes=self.minutes)
        if self.record_id is None and (self.start or self.end):
            self.end = self.end or datetime.now(UTC)
            self.start = self.start or self.end - timedelta(hours=24)
            if not timedelta(0) <= self.end - self.start <= timedelta(days=7):
                raise ValueError("Event window must be ordered and at most seven days")
        return self


def xpath(query):
    parts = []
    if query.level != "all":
        parts.append(f"(Level >= 1 and Level <= {LEVELS[query.level]})")
    for key, value in (("EventID", query.event_id), ("EventRecordID", query.record_id)):
        if value is not None:
            parts.append(f"{key}={value}")
    if query.before_record:
        parts.append(f"EventRecordID < {query.before_record}")
    if query.provider:
        parts.append(f"Provider[@Name='{query.provider}']")
    for op, value in ((">=", query.start), ("<=", query.end)):
        if value:
            stamp = instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
            parts.append(f"TimeCreated[@SystemTime{op}'{stamp}']")
    return "*[System[" + " and ".join(parts or ["EventRecordID > 0"]) + "]]"


def parse_event(xml, message=None):
    if len(xml.encode("utf-8")) > MAX_BYTES or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ValueError("Event XML exceeds safe bounds or contains declarations")
    root = ET.fromstring(xml)
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    system = root.find("e:System", ns)
    if system is None:
        raise ValueError("Missing System fields")

    def text(name):
        return system.findtext("e:" + name, default=None, namespaces=ns)

    def attr(name, key):
        node = system.find("e:" + name, ns)
        return node.get(key) if node is not None else None

    level = int(text("Level") or 0)
    data = [
        {"name": n.get("Name"), "value": "".join(n.itertext())}
        for n in root.findall("e:EventData/e:Data", ns)
    ]
    timestamp = attr("TimeCreated", "SystemTime")
    if timestamp:
        timestamp = instant(timestamp).isoformat()
    identity = hashlib.sha256(xml.encode("utf-8")).hexdigest()
    return dict(
        evidence_type="windows_event",
        evidence_id="WE-" + identity,
        channel=text("Channel"),
        provider=attr("Provider", "Name"),
        event_id=int(text("EventID")) if text("EventID") else None,
        severity=next((k for k, v in LEVELS.items() if v == level), "unknown"),
        level=level,
        timestamp=timestamp,
        source_timestamp=attr("TimeCreated", "SystemTime"),
        computer=text("Computer"),
        record_id=int(text("EventRecordID")) if text("EventRecordID") else None,
        task=text("Task"),
        opcode=text("Opcode"),
        keywords=text("Keywords"),
        message=message,
        message_status="rendered" if message is not None else "unavailable",
        event_data=data,
        source_xml=xml,
    )


class NativeEvents:
    def __init__(self):
        from ctypes import wintypes as w

        self.dll = ctypes.WinDLL("wevtapi.dll", use_last_error=True)
        H, D, P = w.HANDLE, w.DWORD, ctypes.POINTER
        signatures = {
            "EvtQuery": ([H, w.LPCWSTR, w.LPCWSTR, D], H),
            "EvtNext": ([H, D, P(H), D, D, P(D)], w.BOOL),
            "EvtRender": ([H, H, D, D, w.LPVOID, P(D), P(D)], w.BOOL),
            "EvtClose": ([H], w.BOOL),
            "EvtOpenPublisherMetadata": ([H, w.LPCWSTR, w.LPCWSTR, D, D], H),
            "EvtFormatMessage": ([H, H, D, D, w.LPVOID, D, D, w.LPWSTR, P(D)], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, result

    def read(self, query):
        from ctypes import wintypes as w

        api = self.dll
        handle = api.EvtQuery(None, query.channel, xpath(query), 0x201)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        records, issues = [], []
        total_bytes = 0
        deadline = time.monotonic() + 5
        try:
            # One handle per call: every returned event is closed, including parse errors.
            for _ in range(query.limit):
                if time.monotonic() >= deadline:
                    issues.append("Read deadline reached; results partial")
                    break
                events, count = (w.HANDLE * 1)(), w.DWORD()
                if not api.EvtNext(handle, 1, events, 250, 0, ctypes.byref(count)):
                    code = ctypes.get_last_error()
                    if code == 259:
                        break
                    if code == 1460:
                        issues.append("Read timed out; results partial")
                        break
                    raise ctypes.WinError(code)
                event = events[0]
                try:
                    used, properties = w.DWORD(), w.DWORD()
                    api.EvtRender(
                        None, event, 1, 0, None, ctypes.byref(used), ctypes.byref(properties)
                    )
                    if not 0 < used.value <= MAX_BYTES:
                        issues.append("Oversized event omitted")
                        continue
                    buffer = ctypes.create_unicode_buffer((used.value + 1) // 2)
                    if not api.EvtRender(
                        None,
                        event,
                        1,
                        ctypes.sizeof(buffer),
                        buffer,
                        ctypes.byref(used),
                        ctypes.byref(properties),
                    ):
                        issues.append("Event render unavailable")
                        continue
                    record = parse_event(buffer.value)
                    metadata = api.EvtOpenPublisherMetadata(None, record["provider"], None, 0, 0)
                    if metadata:
                        try:
                            needed = w.DWORD()
                            api.EvtFormatMessage(
                                metadata, event, 0, 0, None, 1, 0, None, ctypes.byref(needed)
                            )
                            if 0 < needed.value <= MAX_BYTES // 2:
                                msg = ctypes.create_unicode_buffer(needed.value)
                                if api.EvtFormatMessage(
                                    metadata,
                                    event,
                                    0,
                                    0,
                                    None,
                                    1,
                                    len(msg),
                                    msg,
                                    ctypes.byref(needed),
                                ):
                                    record["message"] = msg.value
                                    record["message_status"] = "rendered"
                        finally:
                            api.EvtClose(metadata)
                    size = len(json.dumps(record).encode("utf-8"))
                    if total_bytes + size > 1_048_576:
                        issues.append("One MiB response budget reached; narrow filters")
                        break
                    total_bytes += size
                    records.append(record)
                except (ValueError, ET.ParseError):
                    issues.append("Malformed event omitted")
                finally:
                    api.EvtClose(event)
        finally:
            api.EvtClose(handle)
        return records, issues


def read_events(query, native=None):
    result = dict(
        channel=query.channel,
        records=[],
        status="available",
        issues=[],
        query=query.model_dump(mode="json"),
        next_before_record=None,
    )
    if sys.platform != "win32" and native is None:
        return {**result, "status": "unavailable", "reason": "not available on this platform"}
    if not _slots.acquire(blocking=False):
        return {**result, "status": "unavailable", "reason": "Event inspection busy; retry later"}
    try:
        records, issues = (native or NativeEvents()).read(query)
        result.update(records=records[: query.limit], issues=issues)
        if len(records) == query.limit and records[-1]["record_id"]:
            result["next_before_record"] = records[-1]["record_id"]
        return result
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        reason = (
            "permission denied"
            if code == 5
            else "channel unavailable"
            if code == 15007
            else "Windows event API unavailable"
        )
        return {**result, "status": "unavailable", "reason": reason, "error_code": code}
    finally:
        _slots.release()


def channels():
    return {
        "channels": [
            {
                "channel": name,
                **{
                    k: v
                    for k, v in read_events(EventQuery(channel=name, level="all", limit=1)).items()
                    if k in {"status", "reason", "error_code"}
                },
            }
            for name in CHANNELS
        ]
    }


def assemble(records):
    """Adapt measured severity to the shared log contract; never reclassify message text."""
    from collections import defaultdict

    groups = defaultdict(list)
    events = []
    records = sorted(
        records, key=lambda r: (r["timestamp"] or "", r["channel"] or "", r["record_id"] or 0)
    )
    for line, record in enumerate(records, 1):
        text = json.dumps(record, ensure_ascii=False, sort_keys=True)
        events.append(
            dict(
                line=line,
                timestamp=record["timestamp"],
                severity=record["severity"],
                text=text,
                source=record["evidence_id"],
                evidence=record,
            )
        )
        groups[(record["channel"], record["provider"], record["event_id"])].append(record)
    return dict(
        line_count=len(events),
        error_count=sum(r["severity"] in {"error", "critical"} for r in records),
        warning_count=sum(r["severity"] == "warning" for r in records),
        events=events,
        events_truncated=False,
        time_range={
            "first_observed": records[0]["timestamp"] if records else None,
            "last_observed": records[-1]["timestamp"] if records else None,
        },
        repeated_patterns=[
            dict(
                channel=k[0],
                provider=k[1],
                event_id=k[2],
                count=len(v),
                pattern=f"{k[0]} / {k[1]} / {k[2]}",
                evidence_ids=[r["evidence_id"] for r in v],
                timestamps=[r["timestamp"] for r in v],
            )
            for k, v in groups.items()
            if len(v) > 1
        ],
        limitations=(
            "Native event levels; bounded sample only. Repetition is not causation. "
            "Missing messages remain unavailable."
        ),
    )


def correlate(records, storage, service_name, minutes=5):
    """Time proximity only. Inventory association is explicitly operator selected."""
    if not storage or not service_name or service_name == "unknown":
        return []
    service = storage.service(service_name)
    if not service:
        return []
    result = []
    observations = [
        (kind, observation)
        for kind, items in (
            ("http_observation", storage.observations(service["id"], 50)),
            ("dns_observation", storage.dns_observations(service["id"], 50)),
            ("tls_observation", storage.tls_observations(service["id"], 50)),
        )
        for observation in items
    ]
    for kind, obs in observations:
        stamp = obs.get("checked_at") or obs.get("observed_at")
        if not stamp:
            continue
        refs = [
            r["evidence_id"]
            for r in records
            if r["timestamp"]
            and abs((instant(r["timestamp"]) - instant(stamp)).total_seconds()) <= minutes * 60
        ]
        if refs:
            result.append(
                dict(
                    evidence_type=kind,
                    observation_id=obs.get("id"),
                    observation=obs,
                    event_evidence_ids=refs,
                    window_minutes=minutes,
                    relationship=(
                        "Time proximity to operator-selected service; "
                        "host relationship and causation unconfirmed"
                    ),
                )
            )
    return result


async def analyse_events(
    records, question, provider, use_model=False, notes="", service="unknown", storage=None
):
    from app.services.grounding import explain_logs

    evidence = assemble(records)
    interpretation, explanation, quality = None, None, "not_requested"
    if use_model and not local_model_allowed(provider):
        explanation, quality = (
            "Event interpretation requires an entirely local model route.",
            "local_model_required",
        )
    elif use_model:
        # Shared model_events selects at most 12 records / 5000 characters and excludes Security.
        interpretation, explanation, quality = await explain_logs(
            provider, evidence, question, notes
        )
    confirmed = "\n".join(
        f"[L{e['line']} / {e['source']}] {e['evidence']['channel']} / "
        f"{e['evidence']['provider']} / Event ID {e['evidence']['event_id']} / "
        f"{e['severity']} / {e['timestamp']}: "
        f"{e['evidence']['message'] or 'Rendered message unavailable'}"
        for e in evidence["events"]
    )
    if evidence["repeated_patterns"]:
        confirmed += "\nRepeated exact channel/provider/event ID groups:\n" + "\n".join(
            f"{g['pattern']}: {g['count']} records [{', '.join(g['evidence_ids'])}]"
            for g in evidence["repeated_patterns"]
        )
    return {
        **evidence,
        "service": service,
        "interpretation": interpretation,
        "explanation": explanation,
        "quality": quality,
        "answer": "Confirmed:\n"
        + (confirmed or "No matching events in the bounded sample.")
        + "\nProbable:\n"
        + (explanation or "No cause established.")
        + "\nUnknown:\nRoot cause, host/service association and recovery are unconfirmed.",
        "correlations": correlate(records, storage, service),
    }


def local_model_allowed(provider):
    """Fail closed for unknown adapters or any remotely configurable fallback."""
    settings = getattr(provider, "settings", None)
    if settings is None:
        return False
    registry = getattr(provider, "registry", None)
    if registry:
        from app.ai.routing import request_route

        task, override = request_route.get()
        kinds = [registry.get(key).provider for key in registry.candidates(task, override)]
    else:
        kinds = [settings.ai_provider]
    # A loopback daemon can itself forward requests; only in-process inference guarantees locality.
    return bool(kinds) and all(kind == "native" for kind in kinds)


def event_question(message):
    return bool(
        re.search(
            r"windows (?:events?|service)|event (?:log|id)|"
            r"errors? (?:on|happened on) (?:this|my) (?:pc|computer)|"
            r"system errors|critical events|failures from the same source",
            message,
            re.I,
        )
    )


async def event_chat(body, storage, provider=None):
    if not event_question(body.message):
        return None
    from starlette.concurrency import run_in_threadpool

    from app.models import LiveEvidence

    message = body.message.lower()
    names = [name for name in CHANNELS if name.lower() in message]
    names = names or ["System", "Application"]  # Security requires explicit channel selection.
    level = next((name for name in ("critical", "error", "warning") if name in message), "warning")
    number = re.search(r"event id\s+(\d{1,5})\b", message)
    event_id = int(number[1]) if number and int(number[1]) <= 65535 else None
    end = datetime.now(UTC)
    start = end - timedelta(hours=1 if "last hour" in message else 24)
    records, states = [], []
    for channel in names:
        page = await run_in_threadpool(
            read_events,
            EventQuery(
                channel=channel, level=level, event_id=event_id, start=start, end=end, limit=20
            ),
        )
        records.extend(page["records"])
        states.append(
            f"{channel}: {page['status']} ({page.get('reason', 'bounded sample')}); "
            f"{len(page['records'])}/20 records; minimum {level}; "
            f"{start.isoformat()} to {end.isoformat()}; " + "; ".join(page.get("issues", []))
        )
    result = await analyse_events(
        records,
        body.message,
        provider,
        body.explain,
        service=body.service or "unknown",
        storage=storage,
    )
    evidence = [
        LiveEvidence(
            label=f"L{event['line']}",
            evidence_type="windows_event",
            windows_event=event["evidence"],
            observation_id=event["source"],
            service=body.service or "local Windows host",
            observed_at=event["timestamp"],
            state=event["severity"],
            reason="Measured native Windows event; no causal attribution",
        )
        for event in result["events"]
    ]
    return dict(
        answer="\n".join(states) + "\n" + result["answer"],
        sources=[],
        live=evidence,
        grounded=bool(evidence),
        quality=result["quality"] if body.explain else "deterministic",
        interpretation=result["explanation"],
    )
