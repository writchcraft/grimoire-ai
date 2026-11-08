# Explicit-target TCP diagnostics

Grimoire establishes a TCP connection to a known host and port, closes the socket, and sends no application bytes. No model determines connection success.

```powershell
grimoire services tcp Grafana
grimoire services tcp --json
grimoire network tcp example.local 443 --json
```

The services commands use existing inventory only. The network command accepts exactly one host and one integer port; its transient result is not added to service history. There are no range, subnet, discovery, fingerprinting or banner options.

Targets come from `monitor.url`, `monitor.health_url`, HTTP(S) inventory `urls`, and inventory `ports`. URL ports default to 80/443. Additional ports use `hostname`, then `ip_address`, then the monitor URL hostname. Duplicate normalized host/port pairs are removed. URL paths, queries and fragments are never sent or retained in TCP observations. Service URL origins must pass existing network safety validation.

## Bounds and safety

- Three seconds total per endpoint, including DNS. Four concurrent TCP operations per application process; excess calls return unknown/busy. Native DNS workers use the existing independent four-worker bound and retain slots until completion after a timeout.
- At most 32 resolver results; all returned addresses must pass the shared HTTP/TLS safety policy. Private and loopback addresses are allowed. Metadata, link-local, unspecified, multicast, reserved and broadcast addresses are rejected; IPv6 loopback is explicitly allowed.
- Exactly one validated numeric address is chosen, sorted by IP version then numeric value (IPv4 first). The numeric socket address and explicit address family prevent a second lookup. Failure of that address is not a statement about every address for the hostname.
- At most 50 configured endpoints per request/batch. Service batches run sequentially. Concurrent inspection of the same service is refused. A five-second cooldown per service/target reuses the original observation ID and timestamp; it does not fabricate a new check. Bounds and reservations are per process, not a distributed rate limiter.
- Sockets close on success, error or cancellation. No authentication, shell execution, service control, application exchange, firewall change or other network modification is available.

## Observation contract

Each `tcp_observation` has an ID, service ID/name, hostname, pinned address when available, port, deterministic state, connection success, connection latency in milliseconds, total latency including resolution, UTC observation/check timestamp, bounded failure category and safe reason. Connection latency is null when no connect was attempted. Error text from the network is not echoed.

States: `reachable`, `refused`, `timeout`, `resolution-failed`, `blocked/unreachable`, `invalid`, `unknown`. The failure category distinguishes safety-policy refusal, observed host/network unreachability and local access denial. Busy capacity is unknown, not endpoint failure. A timeout does not prove a firewall blocked traffic; refusal does not prove a process crashed.

## API and Services view

`POST /api/services/{id}/tcp` accepts no target overrides (an empty JSON object or omitted body). It returns the observations for that service's configured targets. Unknown body fields return 422. Unknown service IDs return 404. `GET /api/services/{id}/tcp` returns `targets`, current-target `latest` observations, and bounded `history`. Reading performs no probe. `/api/services?health=true` attaches the same TCP summary.

The Services card has an Inspect TCP button and displays port, state, pinned address, connection latency, elapsed age, exact timestamp, staleness after five minutes and up to five retained samples per endpoint. Empty observations remain unknown. This adds no scanner interface. A monitored target failing does not affect Grimoire's readiness or doctor status.

## Evidence and diagnostic layers

Live questions use deterministic TCP evidence labelled C1, C2, etc. `probe: true` permits a fresh bounded inspection; otherwise answers use retained evidence and label stale samples. Static questions asking which port is configured stay on the knowledge path. Explicit service context also supports follow-up questions such as “What changed around the failure?”

DNS, TCP, TLS and HTTP keep separate source identities, timestamps and histories. The answer displays their independent diagnostic layers and does not imply a single atomic transaction. HTTP/TLS layer conclusions require a matching hostname and port. A reachable TCP observation plus an HTTP 500 identifies separate transport success and an application server error. Reachable TCP plus a recorded TLS hostname mismatch identifies the TLS sample as a failing layer. Different DNS choices, times, paths and caches remain possible; a successful old sample does not prove current reachability.

Only `local_windows_service` creates a service mapping. A requested fresh probe also reads that exact mapped Windows service; retained snapshots always retain their source evidence ID and timestamp and are not called current. Host correlation can link refused/timed-out TCP observations to a stopped explicitly mapped service without asserting causation. Generic names never create mappings.

Windows event/incident correlation now includes TCP observation IDs. Change questions can display at most 20 local System events within five minutes either side of the first selected TCP sample. The scope is explicitly local and time-based; the endpoint may be on another host. Events do not establish a cause. Raw nearby events are excluded from optional transport model interpretation, preserving native-event privacy rules.

## History and validation

SQLite retains at most 50 TCP observations per service and 5,000 globally. Limits apply across that service's endpoints. Deleting a service removes its TCP records. Changes to inventory do not make an old target's observation the latest result for a new target. Sparse manual observations are never converted into uptime percentages.

Run `.venv/Scripts/python scripts/tcp_acceptance.py` for disposable local IPv4/IPv6, hostname, refusal, timeout-backlog, configured HTTP, cooldown, history, API and CLI acceptance. It writes `docs/tcp-evidence/local.json`. No public or unrelated LAN targets are needed. Run `scripts/check.py` for the repository gate and the existing native-model `evaluate --suite all` command for development/held-out reliability.
