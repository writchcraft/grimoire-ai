"""Bounded, read-only TLS measurement. No HTTP data or credentials are sent."""

import asyncio
import ipaddress
import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from starlette.concurrency import run_in_threadpool
from urllib3.util.ssl_match_hostname import CertificateError, match_hostname

from app.services.dns import system_lookup, validate_hostname
from app.services.probe import ProbeError, blocked_address, check_url, clean

EXPIRING_DAYS = 30
MAX_CERT_BYTES = 65536
_slots = threading.BoundedSemaphore(4)


def target(service):
    url = (service.get("monitor") or {}).get("url")
    if not url:
        return None
    check_url(url)
    parts = urlsplit(url)
    return (parts.hostname, parts.port or 443) if parts.scheme == "https" else None


def certificate_facts(der, host, now, expiring_days=EXPIRING_DAYS):
    if len(der) > MAX_CERT_BYTES:
        raise ValueError("Certificate exceeds limit")
    cert = x509.load_der_x509_certificate(der)
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [
            ("DNS", n.value) if isinstance(n, x509.DNSName) else ("IP Address", str(n.value))
            for n in names
            if isinstance(n, (x509.DNSName, x509.IPAddress))
        ]
    except x509.ExtensionNotFound:
        sans = []
    entries = []
    try:
        all_names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        all_names = []
    if len(all_names) > 256:
        raise ValueError("Certificate names exceed limit")
    for name in all_names:
        if isinstance(name, x509.DNSName):
            kind, value = "DNS", name.value
        elif isinstance(name, x509.IPAddress):
            kind, value = "IP Address", str(name.value)
        elif isinstance(name, x509.OtherName):
            kind, value = "Other Name", name.type_id.dotted_string + ":" + name.value.hex()
        elif isinstance(name, x509.DirectoryName):
            kind, value = "Directory Name", name.value.rfc4514_string()
        elif isinstance(name, x509.RegisteredID):
            kind, value = "Registered ID", name.value.dotted_string
        else:
            kind, value = type(name).__name__, str(name.value)
        if len(value) > 1024:
            raise ValueError("Certificate names exceed limit")
        entries.append({"type": kind, "value": clean(value, 1024)})
    if len(sans) > 256 or any(len(v) > 1024 for _, v in sans):
        raise ValueError("Certificate names exceed limit")
    common = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    mapping = {"subjectAltName": sans, "subject": tuple((("commonName", n.value),) for n in common)}
    try:
        match_hostname(mapping, host)
        matched = "match"
    except CertificateError:
        matched = "mismatch"
    if not sans and not common:
        matched = "unavailable"
    start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
    validity = "not-yet-valid" if now < start else "expired" if now >= end else "valid"
    remaining = (end - now).total_seconds()
    return dict(
        subject=clean(cert.subject.rfc4514_string(), 4096),
        issuer=clean(cert.issuer.rfc4514_string(), 4096),
        serial_number=format(cert.serial_number, "x"),
        fingerprint_sha256=cert.fingerprint(hashes.SHA256()).hex(),
        valid_from=start.isoformat(),
        expires_at=end.isoformat(),
        remaining_seconds=round(remaining),
        remaining_days=round(remaining / 86400, 2),
        sans=entries,
        hostname_match=matched,
        validity=validity,
        expiring_soon=0 < remaining <= expiring_days * 86400,
    )


async def exchange(ip, port, host, context):
    writer = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection(
                str(ip), port, ssl=context, server_hostname=host, ssl_handshake_timeout=3
            )
        tls = writer.get_extra_info("ssl_object")
        return tls.getpeercert(binary_form=True), tls.version(), tls.cipher()
    finally:
        if writer:
            # Abort the inspection transport: do not retain a peer during TLS shutdown.
            writer.transport.abort()


async def inspect_tls(host, port=443, timeout=8.0, trust_context=None, expiring_days=EXPIRING_DAYS):  # noqa: ASYNC109
    started = time.monotonic()
    result = dict(
        hostname=host,
        port=port,
        state="unknown",
        applicable=True,
        connection_success=False,
        certificate_presented=False,
        trust="unavailable",
        trust_reason=None,
        trust_verify_code=None,
        hostname_match="unavailable",
        validity="unknown",
        protocol=None,
        cipher=None,
        error=None,
        reason="No TLS result.",
        expiring_days=expiring_days,
    )
    acquired = False
    try:
        async with asyncio.timeout(timeout):
            acquired = _slots.acquire(blocking=False)
            if not acquired:
                raise ProbeError("busy", "TLS inspection capacity reached; retry later.")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Invalid port")
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                host = validate_hostname(host)
                infos = await system_lookup(host)
                addresses = list(dict.fromkeys(ipaddress.ip_address(i[4][0]) for i in infos[:32]))
                if len(infos) > 32:
                    raise ProbeError("blocked_target", "Too many resolver results") from None
            if not addresses:
                raise ProbeError("dns_failure", "Hostname did not resolve")
            if any(blocked_address(ip) for ip in addresses):
                raise ProbeError("blocked_target", "Target refused by network safety policy")
            # The same validated IP and SNI are used for both measurement handshakes.
            ip = addresses[0]
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            der, protocol, cipher = await exchange(ip, port, host, context)
            result.update(
                connection_success=True,
                certificate_presented=bool(der),
                protocol=protocol,
                cipher=cipher,
                address=str(ip),
            )
            result.update(certificate_facts(der, host, datetime.now(UTC), expiring_days))
            context = trust_context or runtime_context()
            # Chain/date validation is deliberately independent of hostname matching.
            if context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise ValueError(
                    "Trust context must require validation and disable hostname checking"
                )
            try:
                verified, _, _ = await exchange(ip, port, host, context)
                if verified != der:
                    result.update(
                        trust="unavailable", trust_reason="Certificate changed between handshakes"
                    )
                else:
                    result.update(
                        trust="trusted", trust_reason="Local runtime validation succeeded"
                    )
            except ssl.SSLCertVerificationError as exc:
                result.update(
                    trust="failed",
                    trust_verify_code=exc.verify_code,
                    trust_reason=clean(exc.verify_message, 300),
                )
            except (OSError, TimeoutError):
                result.update(trust="unavailable", trust_reason="Validation handshake failed")
            state = result["validity"]
            if state == "valid":
                state = (
                    "hostname-mismatch"
                    if result["hostname_match"] == "mismatch"
                    else "untrusted"
                    if result["trust"] == "failed"
                    else "unknown"
                    if result["trust"] != "trusted" or result["hostname_match"] != "match"
                    else "expiring-soon"
                    if result["expiring_soon"]
                    else "valid"
                )
            result.update(
                state=state,
                reason="Certificate presented; see separate validity, trust and hostname results.",
            )
    except TimeoutError:
        result.update(error="timeout", reason="TLS inspection deadline exceeded.")
    except ProbeError as exc:
        result.update(error=exc.kind, reason=exc.message)
    except (ValueError, TypeError):
        result.update(error="invalid", reason="Invalid target or bounded certificate data.")
    except socket.gaierror:
        result.update(error="dns_failure", reason="Hostname did not resolve.")
    except BlockingIOError:
        result.update(error="resolver_busy", reason="Resolver capacity reached; retry later.")
    except OSError:
        result.update(error="handshake_failed", reason="TCP/TLS connection failed.")
    finally:
        if acquired:
            _slots.release()
        if not result["connection_success"] and result["error"] in {"timeout", "handshake_failed"}:
            result["state"] = "handshake-failed"
        result["observed_at"] = datetime.now(UTC).isoformat()
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def runtime_context():
    context = ssl.create_default_context()
    context.check_hostname = False
    return context


async def inspect_and_record(storage, service):
    pair = target(service)
    history = await run_in_threadpool(storage.tls_observations, service["id"], 1)
    if (
        history
        and (datetime.now(UTC) - datetime.fromisoformat(history[0]["observed_at"])).total_seconds()
        < 5
    ):
        return history[0]
    if pair:
        result = await inspect_tls(*pair, trust_context=runtime_context())
    else:
        result = dict(
            state="unknown",
            applicable=False,
            hostname=None,
            port=None,
            reason="No configured HTTPS monitor; TLS inspection skipped.",
            observed_at=datetime.now(UTC).isoformat(),
            latency_ms=0,
        )
    result.update(
        id=str(uuid4()),
        service_id=service["id"],
        service=service["name"],
        checked_at=result["observed_at"],
    )
    await run_in_threadpool(storage.record_tls, result)
    return result


async def inspect_many(storage, services):
    targets = [s for s in services if target(s)]
    if len(targets) > 50:
        raise ValueError("Batch limit is 50 services; inspect individually")
    return [await inspect_and_record(storage, s) for s in targets]


def summary(storage, service):
    history = storage.tls_observations(service["id"], 10)
    changes = None
    if len(history) > 1:
        a, b = history[:2]
        if (a.get("hostname"), a.get("port")) == (b.get("hostname"), b.get("port")) and all(
            o.get("fingerprint_sha256") for o in (a, b)
        ):
            changes = {
                k: a.get(k) != b.get(k)
                for k in ("fingerprint_sha256", "issuer", "sans", "expires_at", "trust")
            }
    return dict(latest=history[0] if history else None, history=history, changes=changes)
