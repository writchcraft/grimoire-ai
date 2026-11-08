"""Explicit-target TCP connect only: no writes, authentication or discovery."""

import asyncio
import errno
import ipaddress
import socket
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from app.services.dns import system_lookup, validate_hostname
from app.services.probe import ProbeError, blocked_address, check_url

TIMEOUT = 3.0
MAX_BATCH = 50
COOLDOWN = 5
_slots = threading.BoundedSemaphore(4)
_recording = set()
_guard = threading.Lock()


def targets(service):
    """Reuse inventory; additional ports belong only to its explicit host/IP."""
    pairs = []
    monitor = service.get("monitor") or {}
    for url in [monitor.get("url"), monitor.get("health_url"), *service.get("urls", [])]:
        if url:
            parts = urlsplit(url)
            check_url(parts._replace(path="", query="", fragment="").geturl())
            pairs.append((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)))
    host = (
        service.get("hostname")
        or service.get("ip_address")
        or (urlsplit(monitor["url"]).hostname if monitor.get("url") else None)
    )
    if host:
        pairs.extend((str(host), port) for port in service.get("ports", []))
    normalized = []
    for host, port in pairs:
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                host = validate_hostname(host) + ("." if host.endswith(".") else "")
            except ValueError:
                pass  # Probe records an invalid configured hostname without connecting.
        normalized.append((host, port))
    pairs = list(dict.fromkeys(normalized))
    if len(pairs) > MAX_BATCH:
        raise ValueError("TCP batch limit is 50 explicit endpoints")
    return pairs


async def connect(address, port):
    # Numeric address and explicit family avoid a second resolver call/rebinding.
    family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(
            sock, (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        )
    finally:
        sock.close()


async def inspect_tcp(host, port, timeout=TIMEOUT):  # noqa: ASYNC109
    started = time.perf_counter()
    result = dict(
        hostname=host,
        address=None,
        port=port,
        state="unknown",
        connection_success=False,
        error_category=None,
        reason="No TCP result.",
        connection_latency_ms=None,
    )
    acquired = False
    phase = "resolution"
    try:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Invalid port")
        if not isinstance(host, str) or not host or "%" in host:
            raise ValueError("Invalid host")
        if not 0 < timeout <= TIMEOUT:
            raise ValueError("Invalid deadline")
        acquired = _slots.acquire(blocking=False)
        if not acquired:
            raise ProbeError("busy", "TCP capacity reached; retry later.")
        async with asyncio.timeout(timeout):
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                absolute = host.endswith(".")
                host = validate_hostname(host) + ("." if absolute else "")
                result["hostname"] = host
                infos = await system_lookup(host)
                if len(infos) > 32:
                    raise ProbeError(
                        "blocked_target", "Resolver results exceed safety bound."
                    ) from None
                addresses = []
                for family, _, _, _, sockaddr in infos:
                    if family not in {socket.AF_INET, socket.AF_INET6} or "%" in sockaddr[0]:
                        raise ValueError("Invalid resolver address") from None
                    ip = ipaddress.ip_address(sockaddr[0])
                    if ip.version != (4 if family == socket.AF_INET else 6):
                        raise ValueError("Invalid resolver family") from None
                    addresses.append(ip)
            if not addresses:
                raise socket.gaierror()
            if any(blocked_address(ip) for ip in addresses):
                raise ProbeError("blocked_target", "Target refused by network safety policy.")
            # One deterministic address only; result is scoped to this pinned address.
            address = str(sorted(set(addresses), key=lambda ip: (ip.version, int(ip)))[0])
            result["address"] = address
            phase = "connection"
            connection_started = time.perf_counter()
            try:
                await connect(address, port)
            finally:
                result["connection_latency_ms"] = round(
                    (time.perf_counter() - connection_started) * 1000, 2
                )
            result.update(
                state="reachable",
                connection_success=True,
                reason="TCP connection established; no application payload sent.",
            )
    except TimeoutError:
        result.update(
            state="timeout",
            error_category="timeout",
            reason=f"TCP {phase} deadline exceeded; cause unconfirmed.",
        )
    except socket.gaierror:
        result.update(
            state="resolution-failed",
            error_category="dns_failure",
            reason="Hostname did not resolve.",
        )
    except ProbeError as exc:
        result.update(
            state="blocked/unreachable" if exc.kind == "blocked_target" else "unknown",
            error_category=exc.kind,
            reason=exc.message,
        )
    except (ValueError, TypeError):
        result.update(state="invalid", error_category="invalid", reason="Invalid single host/port.")
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        state, category, reason = (
            ("refused", "connection_refused", "TCP connection refused; cause unconfirmed.")
            if isinstance(exc, ConnectionRefusedError) or code in {errno.ECONNREFUSED, 10061}
            else ("timeout", "timeout", "TCP connection timed out; cause unconfirmed.")
            if code in {errno.ETIMEDOUT, 10060}
            else ("blocked/unreachable", "host_unreachable", "Host/network unreachable.")
            if code in {errno.EHOSTUNREACH, errno.ENETUNREACH, 10051, 10065}
            else ("blocked/unreachable", "access_denied", "Connection denied by local policy.")
            if code in {errno.EACCES, errno.EPERM, 10013}
            else ("unknown", "busy", "Resolver capacity reached; retry later.")
            if isinstance(exc, BlockingIOError)
            else ("unknown", "connection_failed", "TCP connection failed; cause unconfirmed.")
        )
        result.update(state=state, error_category=category, reason=reason)
    finally:
        if acquired:
            _slots.release()
        result["observed_at"] = datetime.now(UTC).isoformat()
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return result


async def inspect_and_record(storage, service):
    pairs = targets(service)
    key = service["id"]
    with _guard:
        if key in _recording:
            raise ValueError("TCP inspection already in progress; retry later")
        _recording.add(key)
    try:
        history = await run_in_threadpool(storage.tcp_observations, key, 50)
        results = []
        for host, port in pairs:
            recent = next(
                (
                    o
                    for o in history
                    if (o["hostname"], o["port"]) == (host, port)
                    and (
                        datetime.now(UTC) - datetime.fromisoformat(o["observed_at"])
                    ).total_seconds()
                    < COOLDOWN
                ),
                None,
            )
            if recent:
                results.append(recent)
                continue
            item = await inspect_tcp(host, port)
            item.update(
                id=str(uuid4()),
                service_id=key,
                service=service["name"],
                checked_at=item["observed_at"],
            )
            await run_in_threadpool(storage.record_tcp, item)
            results.append(item)
        return results
    finally:
        with _guard:
            _recording.discard(key)


async def inspect_many(storage, services):
    if sum(len(targets(s)) for s in services) > MAX_BATCH:
        raise ValueError("TCP batch limit is 50 explicit endpoints; inspect individually")
    results = []
    for service in services:
        results.extend(await inspect_and_record(storage, service))
    return results


def summary(storage, service):
    history = storage.tcp_observations(service["id"], 50)
    pairs = targets(service)
    latest = []
    for host, port in pairs:
        item = next((o for o in history if (o["hostname"], o["port"]) == (host, port)), None)
        if item:
            latest.append(item)
    return dict(
        latest=latest, history=history, targets=[dict(hostname=h, port=p) for h, p in pairs]
    )
