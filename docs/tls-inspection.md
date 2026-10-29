# Read-only TLS inspection

TLS inspection uses the configured monitor URL: `https://grafana.home.arpa:3000/path`
becomes hostname `grafana.home.arpa`, port `3000`, with the same hostname used for
SNI. The URL path is not requested. Plain HTTP and services without HTTPS monitors
are skipped. There is no arbitrary-host API or separate TLS target inventory.

```console
grimoire services tls Grafana
grimoire services tls
grimoire services tls Grafana --json
```

POST `/api/services/{id}/tls` performs inspection. GET at the same path reads the
latest retained observation, ten recent observations, and change flags. Inventory
GET with `health=true` includes the TLS summary. GET never probes. The Services
view has an Inspect TLS button, state, expiry and days remaining at inspection,
protocol, hostname match, trust reason, checked time, stale label and change fields.

The deterministic adapter in `backend/app/services/tls.py` retrieves the leaf
certificate in a dedicated context without chain verification. It sends no HTTP,
credentials or client certificate. This is necessary to inspect expired and
self-signed certificates. It does not alter HTTP verification. A second handshake
to the same pinned address validates against the local runtime's default CA store,
with hostname validation disabled in that context. Certificate identity must agree
between the handshakes before success is called trusted. A certificate change
between successful handshakes leaves trust unavailable. A failed verification
handshake cannot expose its peer certificate; its error is a second-handshake
runtime result, not proof that the first peer's complete chain was identical.

Cryptography parses bounded DER into subject, issuer, serial, SHA-256 fingerprint,
UTC validity start/expiry, remaining seconds/days, SAN entries and date validity.
DNS/IP entries drive matching; email, URI and other SAN forms are retained as typed values.
Hostname matching uses urllib3's maintained matcher (Python's old `ssl.match_hostname`
is unavailable in this runtime), including wildcard, common-name fallback and
literal IP semantics. Names without any usable identity are unavailable. Protocol,
cipher tuple, connected address, observation time and total inspection latency are
measured from the connection. No model decides any field.

Independent dimensions:

| Field | Meaning |
| --- | --- |
| `connection_success` | A TLS handshake completed, regardless of certificate trust. |
| `certificate_presented` | A leaf certificate was retrieved. |
| `trust` | `trusted`, `failed`, or `unavailable`; the local runtime's chain/date verification result, independent of hostname matching. |
| `trust_verify_code`, `trust_reason` | Runtime verification code and bounded reason; e.g. self-signed certificate, unknown issuer or date failure. |
| `hostname_match` | `match`, `mismatch`, or `unavailable`. |
| `validity` | `valid`, `expired`, `not-yet-valid`, or `unknown`, computed from leaf dates and the system clock. |
| `expiring_soon` | Positive remaining lifetime <= 30 days by default. |

The primary state prioritizes expired/not-yet-valid, then hostname mismatch, failed
trust, unavailable validation, expiry warning, and valid. All dimensions remain
visible even when several warnings apply. `untrusted` does not mean down.
`handshake-failed` means no successful TLS connection before a connection/TLS error
or deadline. DNS/policy failures have state unknown with a separate error category.
Missing or unparseable data stays unknown. Runtime trust includes date validation;
it does not separately establish whether an expired chain would otherwise anchor.
The default warning threshold is documented here and recorded as `expiring_days` in
each observation; the deterministic inspection function accepts a threshold for
callers/tests. There is no additional user-facing configuration surface.

Safety limits: eight-second overall deadline including capacity/resolution; three
seconds per connection/handshake and an explicit three-second SSL handshake timeout;
four concurrent inspections; the DNS layer's four bounded resolver workers; at most
32 resolver tuples, rejecting oversized results; 64 KiB leaf DER before parsing;
256 SAN entries, 1,024 characters per SAN and 4,096 per subject/issuer; 50-service
batches; five-second persisted cooldown. TLS library handshake buffering is managed
by Python/OpenSSL; the DER application bound is checked after retrieval, not a
custom wire-protocol allocation limit. No close-notify wait can extend inspection.
The first permitted resolver address is inspected; no multi-address availability
claim is made. All resolved addresses must satisfy the unchanged HTTP address
policy, including rejection of metadata, link-local, reserved, unspecified and
multicast targets. Numeric address pinning prevents a second DNS lookup from
changing the connected host. Loopback IPv4/private addresses follow HTTP policy.

A separate SQLite table uses the existing history architecture: 50 observations
per service, 5,000 globally, transactional pruning, persistence and deletion cleanup.
Two newest comparable certificate samples produce fingerprint, issuer, SAN,
expiry and trust change flags. Target changes or missing certificates produce no
comparison. A changed fingerprint/later expiry is evidence of change, not proof of
a renewal operation. Count retention has no age guarantee.

`tls_observation` evidence has T labels, observation ID, service/hostname, full TLS
facts and retained samples. Deterministic answers cite those IDs/labels. Documentation,
HTTP and DNS remain distinct evidence types. Certificate-only refresh does not
request HTTP. Failure questions may explicitly refresh all three with `probe=true`;
otherwise answers use stored observations only. Each sample keeps its own timestamp,
with a five-minute stale label. Comparison text states that separate samples do not
establish causation. No HTTPS verification bypass is introduced for correlation.
Optional AI interpretation remains in its separate existing response field.

No certificate issuance, ACME, renewal, upload, replacement, trust-store modification,
reverse-proxy settings, restart, shell execution, SSH, Docker access or remediation
capability is exposed by TLS inspection.

For reproducible real socket acceptance, run `python scripts/tls_acceptance.py`
with the backend on PYTHONPATH and project dependencies installed. It creates
throwaway keys/certificates and loopback HTTPS servers, exercises runtime trusted,
untrusted, mismatch, expired, future, soon, plaintext and refused cases plus API,
CLI and chat, then removes private material. The trusted fixture uses an in-memory
SSL context pointing to its disposable certificate; it does not install a root.
Automated TLS tests additionally exercise a stalled handshake/timeout and do not
contact public services. See [acceptance report](tls-report.md).
