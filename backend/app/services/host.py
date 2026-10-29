"""Point-in-time local host facts. No model, subprocess, control or remote transport."""

import hashlib
import ipaddress
import json
import platform
import socket
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field

from app.models import InputModel

_slots = threading.BoundedSemaphore(2)
STATES = Literal[
    "running",
    "stopped",
    "paused",
    "start_pending",
    "stop_pending",
    "pause_pending",
    "continue_pending",
]


class Thresholds(InputModel):
    disk_used_percent: float = Field(default=95, ge=50, le=100, allow_inf_nan=False)
    disk_free_gib: float = Field(default=2, ge=0, le=1024, allow_inf_nan=False)
    memory_used_percent: float = Field(default=90, ge=50, le=100, allow_inf_nan=False)


class ServiceQuery(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    state: STATES | None = None
    start_type: Literal["automatic", "manual", "disabled"] | None = None
    limit: int = Field(default=100, ge=1, le=200)


class HostProvider(Protocol):
    def inspect(self, section: str) -> tuple[dict, list[str]]: ...
    def services(self, query: ServiceQuery, exact: bool) -> tuple[list[dict], list[str], bool]: ...


class WindowsHostProvider:
    """Allowlisted psutil getters, backed by Windows query APIs; never as_dict/process APIs."""

    def __init__(self):
        import psutil

        self.api = psutil

    def inspect(self, section="status"):
        p, facts, issues = self.api, {}, []

        def read(key, fn):
            try:
                facts[key] = fn()
            except (OSError, p.Error, ValueError):
                facts[key] = None
                issues.append(key + " unavailable")

        if section in {"status", "system", "resources"}:
            read(
                "os",
                lambda: dict(
                    name="Windows",
                    release=platform.win32_ver()[0],
                    version=platform.version(),
                    build=sys.getwindowsversion().build,
                    edition=platform.win32_edition(),
                    architecture=platform.machine(),
                ),
            )
            read("boot_time", lambda: datetime.fromtimestamp(p.boot_time(), UTC).isoformat())
            read("uptime_seconds", lambda: max(0, int(time.time() - p.boot_time())))
            read("cpu_model", self.cpu_model)
            read("logical_cpu_count", p.cpu_count)
            # A bounded real interval avoids psutil's meaningless initial nonblocking zero.
            read("cpu_percent", lambda: p.cpu_percent(interval=0.1))
            facts["cpu_sample_seconds"] = 0.1
            read("memory", lambda: self.memory(p.virtual_memory()))
        if section in {"status", "disks"}:
            disks = []
            try:
                partitions = p.disk_partitions(all=False)
                if len(partitions) > 64:
                    issues.append("Disk enumeration truncated at 64 volumes")
                for disk in partitions[:64]:
                    # Fixed local volumes only: never probe mapped/remote drives.
                    if "fixed" not in disk.opts.split(","):
                        continue
                    row = dict(identifier=disk.device, filesystem=disk.fstype)
                    try:
                        usage = p.disk_usage(disk.mountpoint)
                        row.update(
                            total_bytes=usage.total,
                            free_bytes=usage.free,
                            used_bytes=usage.used,
                            used_percent=usage.percent,
                        )
                    except (OSError, p.Error):
                        row.update(
                            total_bytes=None, free_bytes=None, used_bytes=None, used_percent=None
                        )
                        issues.append("Disk space unavailable")
                    disks.append(row)
            except (OSError, p.Error):
                issues.append("Disks unavailable")
            facts["disks"] = disks
        if section in {"status", "network"}:
            try:
                stats = p.net_if_stats()
                interfaces = []
                entries = sorted(p.net_if_addrs().items())
                if len(entries) > 128:
                    issues.append("Interface enumeration truncated at 128 interfaces")
                for name, addresses in entries[:128]:
                    ips = [
                        a.address
                        for a in addresses
                        if a.family in (socket.AF_INET, socket.AF_INET6)
                    ]
                    if len(ips) > 32:
                        issues.append("Interface addresses truncated at 32 addresses")
                    ips = ips[:32]
                    interfaces.append(
                        dict(
                            identifier=name,
                            state=("up" if stats[name].isup else "down") if name in stats else None,
                            addresses=ips,
                            loopback=bool(ips)
                            and all(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in ips),
                        )
                    )
                facts["interfaces"] = interfaces
            except (OSError, p.Error, ValueError):
                facts["interfaces"] = None
                issues.append("Interfaces unavailable")
        return facts, issues

    @staticmethod
    def cpu_model():
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            0,
            winreg.KEY_READ,
        ) as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()

    @staticmethod
    def memory(value):
        return dict(
            total_bytes=value.total,
            available_bytes=value.available,
            used_bytes=value.total - value.available,
            used_percent=value.percent,
        )

    def services(self, query, exact=False):
        p, records, issues = self.api, [], []
        deadline = time.monotonic() + 5
        try:
            if exact:
                try:
                    services = [p.win_service_get(query.name)]
                except (OSError, p.AccessDenied):
                    # psutil's exact getter reads config to obtain the display name.
                    # Enumeration can still expose status when config access is denied.
                    services = p.win_service_iter()
            else:
                services = p.win_service_iter()
            for index, service in enumerate(services):
                if index >= 4096 or time.monotonic() > deadline:
                    issues.append("Enumeration budget reached; narrow filters")
                    return records, issues, True
                name = service.name()
                if query.name and (
                    name.casefold() != query.name.casefold()
                    if exact
                    else query.name.casefold() not in name.casefold()
                ):
                    continue
                row = dict(name=name)
                for field, getter in (
                    ("display_name", "display_name"),
                    ("state", "status"),
                    ("start_type", "start_type"),
                    ("process_id", "pid"),
                ):
                    try:
                        row[field] = getattr(service, getter)()
                    except (OSError, p.Error):
                        row[field] = None
                        issues.append(f"{name}: {field} unavailable")
                # Account names, binary paths and command lines are deliberately omitted.
                if query.state and row["state"] != query.state:
                    continue
                if query.start_type and row["start_type"] != query.start_type:
                    continue
                if len(records) == query.limit:
                    return records, issues[:200], True
                records.append(row)
                if exact:
                    return records, issues[:200], False
        except p.NoSuchProcess:
            pass
        except (OSError, p.Error):
            issues.append("Service enumeration unavailable")
        return records, issues[:200], False


def evidence(kind, host, timestamp, identifier, facts):
    record = dict(
        evidence_type=kind, host=host, observed_at=timestamp, identifier=identifier, facts=facts
    )
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    return {**record, "evidence_id": ("HO-" if kind == "host_observation" else "WS-") + digest}


def inspect_host(section="status", query=None, exact=False, provider=None, thresholds=None):
    if section not in {"status", "system", "resources", "disks", "network", "services"}:
        raise ValueError("Unknown host section")
    thresholds = thresholds or Thresholds()
    query = query or ServiceQuery()
    now, host = datetime.now(UTC).isoformat(), socket.gethostname()
    result = dict(
        status="unavailable",
        host=host,
        observed_at=now,
        records=[],
        issues=[],
        truncated=False,
        thresholds=thresholds.model_dump(),
    )
    if provider is None and sys.platform != "win32":
        return {**result, "issues": ["Windows host provider unavailable on this platform"]}
    if not _slots.acquire(blocking=False):
        return {**result, "issues": ["Host inspection busy; retry later"]}
    try:
        provider = provider or WindowsHostProvider()
        if section == "services":
            rows, issues, truncated = provider.services(query, exact)
            records = [
                evidence("windows_service_observation", host, now, r["name"], r) for r in rows
            ]
        else:
            facts, issues = provider.inspect(section)
            for disk in facts.get("disks", []):
                disk["space_state"] = (
                    "unavailable"
                    if disk["free_bytes"] is None
                    else (
                        "low"
                        if disk["free_bytes"] <= thresholds.disk_free_gib * 2**30
                        or disk["used_percent"] >= thresholds.disk_used_percent
                        else "normal"
                    )
                )
            memory = facts.get("memory")
            if memory:
                memory["state"] = (
                    "high" if memory["used_percent"] >= thresholds.memory_used_percent else "normal"
                )
            records = [evidence("host_observation", host, now, section, facts)]
            truncated = False
        return {
            **result,
            "status": "partial" if issues else "available",
            "records": records,
            "issues": issues,
            "truncated": truncated,
        }
    except (ImportError, OSError):
        return {**result, "issues": ["Windows host query dependency or API unavailable"]}
    finally:
        _slots.release()


def correlations(host_records, event_records=(), http=None, mapped_name=None):
    """Exact identifiers and timestamps only. Temporal support never establishes cause."""
    links = []
    for record in host_records:
        facts = record["facts"]
        for event in event_records:
            if (event.get("computer") or "").rstrip(".").casefold() != record["host"].casefold():
                continue
            try:
                event_time = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                observed = datetime.fromisoformat(record["observed_at"])
                near = abs((observed - event_time).total_seconds()) <= 300
                boot = facts.get("boot_time")
                startup = (
                    boot
                    and event.get("event_id") in {12, 6005}
                    and event.get("provider") in {"Microsoft-Windows-Kernel-General", "EventLog"}
                    and abs((event_time - datetime.fromisoformat(boot)).total_seconds()) <= 300
                )
            except (ValueError, TypeError, KeyError):
                continue
            identifiers = {d.get("value", "").casefold() for d in event.get("event_data", [])}
            stopped = (
                near
                and facts.get("state") == "stopped"
                and event.get("provider") == "Service Control Manager"
                and event.get("event_id") in {7031, 7034, 7036}
                and bool(
                    {facts.get("name", "").casefold(), facts.get("display_name", "").casefold()}
                    - {""}
                    & identifiers
                )
            )
            if startup or stopped:
                links.append(
                    dict(
                        evidence_ids=[record["evidence_id"], event["evidence_id"]],
                        relationship="Startup event is consistent with the measured boot time"
                        if startup
                        else "Stopped service and nearby matching SCM event",
                        caveat="Supporting evidence; cause and recovery unknown",
                    )
                )
        if (
            http
            and mapped_name
            and facts.get("name", "").casefold() == mapped_name.casefold()
            and facts.get("state") == "stopped"
            and http.get("state") == "unreachable"
        ):
            # Old endpoint observations are explicitly retained as historical, never 'current'.
            links.append(
                dict(
                    evidence_ids=[record["evidence_id"], http["id"]],
                    relationship="Configured HTTP endpoint was unreachable at "
                    + http["checked_at"]
                    + "; mapped local Windows service is stopped at "
                    + record["observed_at"],
                    caveat="Causation unknown",
                )
            )
    return links[:100]


def assessment(records):
    confirmed, probable = [], []
    for record in records:
        facts, ref = record["facts"], record["evidence_id"]
        for disk in facts.get("disks", []):
            if disk.get("space_state") == "low":
                confirmed.append(f"{disk['identifier']}: {disk['free_bytes']} bytes free [{ref}]")
                probable.append("Low free space may contribute to application failures.")
        if (facts.get("memory") or {}).get("state") == "high":
            confirmed.append(f"Memory utilisation: {facts['memory']['used_percent']}% [{ref}]")
            probable.append("Low available memory may contribute to application failures.")
        if facts.get("state") == "stopped":
            confirmed.append(f"{facts['name']} is stopped [{ref}]")
        if facts.get("uptime_seconds") is not None and facts["uptime_seconds"] <= 600:
            confirmed.append(f"Uptime: {facts['uptime_seconds']} seconds [{ref}]")
    return dict(
        confirmed=confirmed,
        probable=sorted(set(probable)),
        unknown=[
            "No evidence proves these observations caused this incident.",
            "Sparse explicit samples do not establish uptime trends.",
        ],
    )


def human(result):
    lines = ["THIS HOST", result["host"], result["status"], result["observed_at"]]
    for record in result["records"]:
        facts = record["facts"]
        os = facts.get("os")
        if os:
            lines.append(
                f"Windows {os['release']} {os['edition']} / Build {os['build']} "
                f"/ {os['architecture']}"
            )
        uptime = facts.get("uptime_seconds")
        if uptime is not None:
            lines.append(
                f"Uptime: {uptime // 86400}d {uptime % 86400 // 3600}h {uptime % 3600 // 60}m"
            )
            lines.append(
                f"CPU: {facts.get('cpu_model') or 'unavailable'} / "
                f"{facts.get('logical_cpu_count')} logical processors / "
                f"{facts.get('cpu_percent')}% (0.1 s sample)"
            )
        memory = facts.get("memory")
        if memory:
            lines.append(
                f"Memory: {gib(memory['used_bytes'])} / {gib(memory['total_bytes'])} "
                f"used / {memory['used_percent']}% / {memory['state']}"
            )
        for disk in facts.get("disks", []):
            lines.append(
                f"{disk['identifier']} {gib(disk['free_bytes'])} / "
                f"{gib(disk['total_bytes'])} free / {disk['used_percent']}% used / "
                f"{disk['space_state']}"
            )
        for nic in facts.get("interfaces") or []:
            lines.append(
                f"{nic['identifier']}: {nic['state'] or 'unavailable'} / "
                + ", ".join(nic["addresses"])
            )
        if "name" in facts:
            lines.append(
                f"{facts['name']} ({facts['display_name']}): "
                f"{facts['state']} / {facts['start_type']} / PID {facts['process_id']}"
            )
    lines.extend(result["issues"])
    if result.get("truncated"):
        lines.append("Results truncated; narrow filters.")
    return "\n".join(lines)


def gib(value):
    return "unavailable" if value is None else f"{value / 2**30:.1f} GiB"


def configured_thresholds(settings):
    return Thresholds(
        disk_used_percent=settings.host_disk_used_percent,
        disk_free_gib=settings.host_disk_free_gib,
        memory_used_percent=settings.host_memory_used_percent,
    )


def retain(directory, result):
    """50 explicit inspection pages total in authoritative SQLite; never a sampling loop."""
    import sqlite3

    if not result["records"]:
        return
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "grimoire.sqlite3", timeout=5)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS host_inspections "
            "(id INTEGER PRIMARY KEY, record TEXT NOT NULL)"
        )
        db.execute("INSERT INTO host_inspections(record) VALUES (?)", (json.dumps(result),))
        db.execute(
            "DELETE FROM host_inspections WHERE id NOT IN "
            "(SELECT id FROM host_inspections ORDER BY id DESC LIMIT 50)"
        )


def history(directory, limit=20):
    import sqlite3

    path = directory / "grimoire.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='host_inspections'").fetchone():
            return []
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT record FROM host_inspections ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 50)),),
            )
        ]


def inspect_and_retain(settings, section="status", query=None, exact=False):
    result = inspect_host(section, query, exact, thresholds=configured_thresholds(settings))
    retain(settings.data_dir, result)
    return result


def context(settings, storage=None, service=None, events=()):
    page = inspect_and_retain(settings)
    records = list(page["records"])
    http, mapping = None, None
    configured = storage.service(service) if storage and service and service != "unknown" else None
    if configured and configured.get("local_windows_service"):
        mapping = configured["local_windows_service"]
        services = inspect_and_retain(settings, "services", ServiceQuery(name=mapping), True)
        records.extend(services["records"])
        page["issues"].extend(services["issues"])
        observations = storage.observations(configured["id"], 1)
        http = observations[0] if observations else None
    return dict(
        records=records,
        issues=page["issues"],
        assessment=assessment(records),
        correlations=correlations(records, events, http, mapping),
    )
