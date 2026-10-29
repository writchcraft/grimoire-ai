"""Read-only HTTP(S) service probe.

Deterministic: every field comes from the network exchange itself. No model is involved and no
response body is stored or interpreted. Monitored endpoints are untrusted input.

Safety contract:
- GET only; no credentials, cookies, proxies or environment configuration are sent or used.
- Overall deadline per endpoint, bounded redirects, bounded body read, identity encoding.
- Loopback and private addresses are allowed (this is a homelab monitor). Link-local (including
  cloud metadata), unspecified, multicast and broadcast addresses are refused, for the initial URL,
  every redirect hop and every resolved address. Connections are pinned to the validated address.
"""

import asyncio
import ipaddress
import re
import socket
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

MAX_BODY_BYTES = 65_536
MAX_REDIRECTS = 3
MAX_ADDRESSES = 3
USER_AGENT = "Grimoire-Monitor/0.1 (read-only)"
STATES = ("healthy", "degraded", "unhealthy", "unreachable", "unknown")
REDIRECT_CODES = {301, 302, 303, 307, 308}

_BLOCKED_EXTRA = (
    ipaddress.ip_address("255.255.255.255"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
)


class ProbeError(Exception):
    """A deterministic, user-safe failure; the message never echoes remote content."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


def blocked_address(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_unspecified
        or ip.is_multicast
        or ip.is_link_local
        or ip.is_reserved
        or ip in _BLOCKED_EXTRA
    )


def check_url(value: str) -> str:
    """Static validation shared by configuration and every probe hop."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("URL must contain 1 to 2048 characters")
    if re.search(r"[\s\x00-\x1f\x7f-\uffff\\]", value):
        raise ValueError("URL must not contain whitespace or control characters")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises on an invalid port
    except ValueError as exc:
        raise ValueError("URL is not valid") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be an absolute http or https URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL must not contain credentials")
    if parts.fragment or parts.query:
        raise ValueError("URL must not contain a query or fragment")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Invalid port")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return value
    if blocked_address(literal):
        raise ValueError("Link-local, unspecified, multicast and broadcast addresses are refused")
    return value


def display_url(value: str | None) -> str | None:
    """Query strings can carry secrets, so observations never store them."""
    if not value:
        return value
    parts = urlsplit(value)
    return parts._replace(query="…" if parts.query else "", fragment="").geturl()


def clean(value: str | None, limit: int = 100) -> str | None:
    if value is None:
        return None
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit] or None


async def resolve(host: str, port: int) -> list:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProbeError("dns_failure", "Hostname did not resolve") from exc
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise ProbeError("dns_failure", "Hostname did not resolve")
    if any(blocked_address(ip) for ip in addresses):
        raise ProbeError("blocked_target", "Hostname resolves to a refused address range")
    return sorted(addresses, key=lambda ip: ip.version)  # IPv4 first


def classify(exc: BaseException) -> ProbeError:
    if isinstance(exc, httpx.TimeoutException):
        return ProbeError("timeout", "Connection timed out")
    chain, seen = exc, set()
    while chain is not None and id(chain) not in seen:
        seen.add(id(chain))
        if isinstance(chain, ssl.SSLError):
            return ProbeError("tls_error", "TLS handshake or certificate verification failed")
        if isinstance(chain, ConnectionRefusedError):
            return ProbeError("connection_refused", "Connection refused")
        chain = chain.__cause__ or chain.__context__
    if isinstance(exc, httpx.ConnectError):
        return ProbeError("connection_failed", "Could not connect")
    if isinstance(exc, httpx.ProtocolError | httpx.DecodingError):
        return ProbeError("protocol_error", "Invalid or interrupted HTTP response")
    return ProbeError("request_failed", "Request failed")


def _empty(url: str) -> dict:
    return {
        "reachable": False,
        "url": None,
        "final_url": None,
        "status_code": None,
        "latency_ms": None,
        "redirects": 0,
        "error": None,
        "error_kind": None,
        "content_type": None,
        "content_length": None,
        "server": None,
        "body_bytes_read": 0,
        "body_truncated": False,
    }


async def _hop(client, url: str, result: dict, addresses: list) -> str | None:
    """One validated, address-pinned GET. Returns a redirect target or None when final."""
    check_url(url)
    parts = urlsplit(url)
    last: ProbeError | None = None
    for ip in addresses[:MAX_ADDRESSES]:
        netloc = f"[{ip}]" if ip.version == 6 else str(ip)
        host_header = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        if parts.port:
            netloc += f":{parts.port}"
            host_header += f":{parts.port}"
        target = f"{parts.scheme}://{netloc}{parts.path or '/'}"
        if parts.query:
            target += "?" + parts.query
        extensions = {}
        if parts.scheme == "https":
            try:
                ipaddress.ip_address(parts.hostname)
            except ValueError:
                extensions["sni_hostname"] = parts.hostname
        try:
            async with client.stream(
                "GET",
                target,
                headers={
                    "Host": host_header,
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
                extensions=extensions,
            ) as response:
                client.cookies.clear()
                result["reachable"] = True
                result["status_code"] = response.status_code
                result["latency_ms"] = round((time.monotonic() - result["_t0"]) * 1000)
                location = response.headers.get("location")
                if response.status_code in REDIRECT_CODES and location:
                    return urljoin(url, location)
                result["final_url"] = display_url(url)
                result["content_type"] = clean(response.headers.get("content-type"))
                result["server"] = clean(response.headers.get("server"))
                declared = response.headers.get("content-length", "")
                result["content_length"] = int(declared) if declared.isdigit() else None
                if (result["content_length"] or 0) > MAX_BODY_BYTES:
                    result["body_truncated"] = True
                    raise ProbeError("response_too_large", "Response exceeds 65536-byte limit")
                total = 0
                async for chunk in response.aiter_raw():
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        result["body_truncated"] = True
                        result["body_bytes_read"] = MAX_BODY_BYTES
                        raise ProbeError("response_too_large", "Response exceeds 65536-byte limit")
                result["body_bytes_read"] = total
                return None
        except httpx.ConnectError as exc:
            last = classify(exc)
            if last.kind == "tls_error":
                raise last from exc
        except httpx.TimeoutException as exc:
            last = classify(exc)
            if not isinstance(exc, httpx.ConnectTimeout):
                raise last from exc
    raise last or ProbeError("connection_failed", "Could not connect")


async def probe_endpoint(
    url: str, timeout_seconds: float = 5.0, verify_tls: bool = True, transport=None
) -> dict:
    """Probe one endpoint. Never raises for remote behaviour; failures are returned data."""
    result = _empty(url)
    started = time.monotonic()
    result["_t0"] = started
    try:
        async with (
            asyncio.timeout(timeout_seconds),
            httpx.AsyncClient(
                verify=verify_tls,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_seconds),
                transport=transport,
            ) as client,
        ):
            check_url(url)
            result["url"] = display_url(url)
            parts = urlsplit(url)
            addresses = await resolve(
                parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
            )
            target = url
            for redirects in range(MAX_REDIRECTS + 1):
                result["redirects"] = redirects
                target = await _hop(client, target, result, addresses)
                if target is None:
                    break
                try:
                    check_url(target)
                except ValueError as exc:
                    raise ProbeError(
                        "blocked_target", "Redirect target refused by safety policy"
                    ) from exc
                original, redirected = urlsplit(url), urlsplit(target)

                def origin(parts):
                    return (
                        parts.scheme,
                        parts.hostname.lower(),
                        parts.port or (443 if parts.scheme == "https" else 80),
                    )

                if origin(original) != origin(redirected):
                    raise ProbeError("cross_origin_redirect", "Cross-origin redirect refused")
            else:
                raise ProbeError("too_many_redirects", f"More than {MAX_REDIRECTS} redirects")
    except ProbeError as exc:
        result["error_kind"], result["error"] = exc.kind, exc.message
    except ValueError:
        result["error_kind"], result["error"] = "invalid_url", "URL refused by safety policy"
    except TimeoutError:
        result["error_kind"], result["error"] = "timeout", f"Timed out after {timeout_seconds:g} s"
    except httpx.HTTPError as exc:
        failure = classify(exc)
        result["error_kind"], result["error"] = failure.kind, failure.message
    except OSError:
        result["error_kind"], result["error"] = "connection_failed", "Could not connect"
    result.pop("_t0", None)
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def derive_state(monitor: dict, primary: dict, health: dict | None) -> tuple[str, str]:
    """Map raw probe results to a health state and a plain reason. Deterministic."""
    expected = monitor["expected_status"]
    if primary["error"]:
        return ("unhealthy" if primary["reachable"] else "unreachable"), f"{primary['error']}."
    if primary["status_code"] != expected:
        return "unhealthy", f"Returned HTTP {primary['status_code']}; expected {expected}."
    if health is not None:
        if health["error"]:
            return (
                "degraded",
                f"Main endpoint answered, but the health endpoint failed: {health['error']}.",
            )
        if health["status_code"] != expected:
            return (
                "degraded",
                f"Main endpoint answered, but the health endpoint returned HTTP "
                f"{health['status_code']}.",
            )
    if primary["latency_ms"] > monitor["slow_ms"]:
        return (
            "degraded",
            f"Answered correctly but slowly ({primary['latency_ms']} ms; threshold "
            f"{monitor['slow_ms']} ms).",
        )
    return "healthy", f"HTTP {expected} as expected in {primary['latency_ms']} ms."


async def probe_service(service: dict, transport=None) -> dict:
    """Probe one stored service record and return a complete observation."""
    monitor = service.get("monitor")
    if not monitor:
        raise ValueError("Service has no monitor configured")
    jobs = [
        probe_endpoint(monitor["url"], monitor["timeout_seconds"], monitor["verify_tls"], transport)
    ]
    if monitor.get("health_url"):
        jobs.append(
            probe_endpoint(
                monitor["health_url"], monitor["timeout_seconds"], monitor["verify_tls"], transport
            )
        )
    results = await asyncio.gather(*jobs)
    primary, health = results[0], results[1] if len(results) > 1 else None
    state, reason = derive_state(monitor, primary, health)
    return {
        "id": str(uuid4()),
        "service_id": service["id"],
        "service": service["name"],
        "environment": monitor.get("environment", ""),
        "checked_at": datetime.now(UTC).isoformat(),
        "state": state,
        "reason": reason,
        "expected_status": monitor["expected_status"],
        "endpoint": primary,
        "health_endpoint": health,
    }
