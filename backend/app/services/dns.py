"""Read-only system name resolution, independent of HTTP connection safety policy.

One AF_UNSPEC getaddrinfo call; no application alias recursion, reverse queries or
connections. OS resolver configuration and caches are respected (including hosts).
The OS owns wire queries/CNAME traversal; only its final canonical name is available.
Timed-out native calls retain their worker slot until completion, bounding orphan work.
"""

import asyncio
import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

MAX_RESULTS = 32
MAX_BATCH = 50
TIMEOUT = 3.0
COOLDOWN = 5.0
_slots = threading.BoundedSemaphore(4)


def hostname_for(service):
    monitor = service.get("monitor")
    return urlsplit(monitor["url"]).hostname if monitor else (service.get("hostname") or None)


def validate_hostname(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("Invalid hostname")
    host = host.removesuffix(".").lower()
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")):
        raise ValueError("Invalid hostname")
    return host


async def system_lookup(host):
    if not _slots.acquire(blocking=False):
        raise BlockingIOError("Resolver capacity reached")
    slots = _slots
    future = Future()

    def resolve():
        try:
            future.set_result(
                socket.getaddrinfo(
                    host,
                    None,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                    0,
                    socket.AI_CANONNAME,
                )
            )
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            slots.release()

    try:
        # A stuck native resolver must not delay CLI process exit. Workers are bounded
        # even after deadlines/cancellation, and never schedule additional queries.
        threading.Thread(target=resolve, name="grimoire-dns", daemon=True).start()
    except BaseException:
        slots.release()
        raise
    return await asyncio.shield(asyncio.wrap_future(future))


async def inspect_hostname(host, lookup=None, timeout=TIMEOUT):  # noqa: ASYNC109
    started = time.monotonic()
    result = dict(
        queried_hostname=host,
        hostname=host,
        status="unknown",
        addresses=[],
        a=[],
        aaaa=[],
        canonical_name=None,
        record_types=[],
        records=[],
        resolver="system",
        error_category=None,
        reason="No resolution result.",
        truncated=False,
        applicable=True,
    )
    try:
        if host is None:
            result.update(applicable=False, reason="No monitor hostname configured.")
            return result
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            result.update(
                applicable=False, reason="Configured URL uses a literal IP; no DNS lookup."
            )
            return result
        absolute = host.endswith(".")
        host = validate_hostname(host)
        query = host + ("." if absolute else "")
        result["queried_hostname"] = query
        result["record_types"] = ["A", "AAAA"]
        result["hostname"] = host
        async with asyncio.timeout(timeout):
            infos = await (lookup or system_lookup)(query)
        addresses = set()
        # Bound iteration even for duplicate/pathological resolver output.
        for family, _, _, canonical, sockaddr in infos[:MAX_RESULTS]:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = ipaddress.ip_address(sockaddr[0].split("%")[0])
            if address.version != (4 if family == socket.AF_INET else 6):
                raise ValueError("Invalid resolver address family")
            addresses.add(address)
            if canonical:
                name = validate_hostname(canonical)
                if name != host:
                    result["canonical_name"] = name
        ordered = sorted(addresses, key=lambda ip: (ip.version, int(ip)))
        result.update(
            addresses=[str(ip) for ip in ordered],
            records=[
                {"type": "A" if ip.version == 4 else "AAAA", "value": str(ip)} for ip in ordered
            ],
            a=[str(ip) for ip in ordered if ip.version == 4],
            aaaa=[str(ip) for ip in ordered if ip.version == 6],
            truncated=len(infos) > MAX_RESULTS,
        )
        result["status"] = (
            "partial" if result["truncated"] else ("resolved" if addresses else "unresolved")
        )
        result["reason"] = (
            "Resolver results truncated; address set may be incomplete."
            if result["truncated"]
            else "System resolver returned addresses."
            if addresses
            else "No addresses returned."
        )
    except TimeoutError:
        result.update(status="timeout", error_category="timeout", reason="Resolution timed out.")
    except socket.gaierror as exc:
        temporary = exc.errno == socket.EAI_AGAIN
        not_found = exc.errno in {
            socket.EAI_NONAME,
            getattr(socket, "EAI_NODATA", socket.EAI_NONAME),
        }
        result.update(
            status="unresolved" if not_found else "unknown",
            error_category="name_not_resolved"
            if not_found
            else ("temporary_failure" if temporary else "resolver_error"),
            reason="Name could not be resolved." if not_found else "Resolver failed; retry later.",
        )
    except ValueError:
        result.update(
            status="invalid",
            error_category="invalid",
            reason="Invalid hostname or resolver result.",
        )
    except OSError as exc:
        result.update(
            error_category="busy" if isinstance(exc, BlockingIOError) else "resolver_error",
            reason="Resolver unavailable; retry later.",
        )
    finally:
        result["observed_at"] = datetime.now(UTC).isoformat()
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


async def inspect_and_record(storage, service, lookup=None):
    # Repeated requests use a recent stored observation, labelled with its original timestamp.
    history = await run_in_threadpool(storage.dns_observations, service["id"], 1)
    if (
        history
        and (datetime.now(UTC) - datetime.fromisoformat(history[0]["observed_at"])).total_seconds()
        < COOLDOWN
    ):
        return history[0]
    result = await inspect_hostname(hostname_for(service), lookup)
    result.update(
        id=str(uuid4()),
        service_id=service["id"],
        service=service["name"],
        checked_at=result["observed_at"],
    )
    await run_in_threadpool(storage.record_dns, result)
    return result


async def inspect_many(storage, services, lookup=None):
    targets = [s for s in services if hostname_for(s)]
    if len(targets) > MAX_BATCH:
        raise ValueError("Batch limit is 50 services; inspect individually")
    # Sequential batches avoid filling worker capacity with our own queued work.
    return [await inspect_and_record(storage, s, lookup) for s in targets]


def address_change(current, previous):
    if not previous or current["hostname"] != previous["hostname"]:
        return None
    if any(o["status"] != "resolved" or not o["applicable"] for o in (current, previous)):
        return None
    return set(current["addresses"]) != set(previous["addresses"])


def summary(storage, service):
    history = storage.dns_observations(service["id"], 10)
    return dict(
        latest=history[0] if history else None,
        history=history,
        addresses_changed=address_change(history[0], history[1]) if len(history) > 1 else None,
    )
