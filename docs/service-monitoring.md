# Manual HTTP service monitoring

Grimoire can observe explicitly configured HTTP(S) services. It has no SSH, Docker API,
command execution, restart, remediation, scheduler, cloud monitoring or telemetry capability.
Inventory entries without a `monitor` remain valid and are never probed.

## Configuration and operation

Use the Services form, `POST /api/services`, or a JSON file:

```console
grimoire services add docs/services.example.json --json
grimoire services list --json
grimoire services probe Grimoire --json
grimoire services probe --json
grimoire services status --json
grimoire chat "Is Grimoire currently online?" --json
```

The [example configuration](services.example.json) uses only loopback addresses. Adjust its
port to your local server. Never configure an endpoint whose GET has side effects. HTTP's
read-only GET convention cannot guarantee how a remote application implements that endpoint.
No private infrastructure is hardcoded. Existing names, kinds, URLs, ports, tags, dependencies
and notes remain supported. An optional `monitor` contains `url`, `health_url`, `expected_status`
(default 200), `timeout_seconds` (0.1–30, default 5), `slow_ms` (default 2000), `environment`, and
`verify_tls` (must be true). Inventory URLs do not automatically become monitoring targets.

`list`, `status`, page loads and ordinary live-state questions read saved observations without
network probes. Use Probe in Services or the CLI to check again. Explicit API chat
`{"message":"Is Grimoire online?","probe":true}` requests a fresh observation. Optional
`explain:true` adds an independent `interpretation` field; it cannot overwrite measured state.
CLI monitoring and deterministic answers require no model inference or internet access.
CLI probe exit status 0 means the observation completed, including observed failures; inspect
the returned health state. Invalid configuration or an unknown target returns a nonzero exit.

## API and UI

Existing service inventory CRUD remains available. Added operations:

| Operation | Behaviour |
| --- | --- |
| `GET /api/services?health=true` | Inventory with the latest saved observation; no probe |
| `POST /api/services/{id}/probe` | Probe the configured service; no arbitrary URL request body |
| `POST /api/services/probe` | Probe configured monitors, maximum 50 targets per request |
| `GET /api/services/{id}/health?history=10` | Latest state plus 1–50 saved observations |

The existing Services view includes optional monitoring configuration, per-service and bulk
Probe buttons, state, status codes, primary/health latency and an explicit check time. Nothing
refreshes or probes on a timer. The original dark/purple visual identity remains intact.

## Deterministic probe and safety

Only GET requests are sent. The probe records reachability, status, elapsed milliseconds,
configured and final URLs, redirect count, observation ID/time, bounded response byte counts,
safe failure categories, and limited sanitized content-type/server/content-length metadata.
It never stores, interprets or executes response bodies. Both the main and optional health
endpoint results remain available in the observation.

Each endpoint has an overall deadline covering DNS, connect, redirects and streaming. At most
three same-origin redirects and 65,536 raw response bytes are allowed. Oversized declared or
streamed responses become explicit errors. Identity encoding avoids decompression of hostile
content. Redirect response bodies are not consumed. HTTP parser limits also bound headers.

URL validation requires HTTP(S), a hostname and a valid port, and rejects credentials, query
strings, fragments, whitespace, control characters and backslashes. Query authentication is
deliberately unsupported. Link-local/cloud metadata, unspecified, multicast and reserved
addresses are blocked, including IPv4-mapped IPv6 and known metadata addresses. All DNS
results are checked; validated IPs are pinned for the probe and redirects, preserving Host and
TLS SNI/certificate validation. Cross-origin redirects and HTTPS downgrades are refused.
Private and loopback addresses are intentionally supported for homelab use. This is a
configured-target policy, not a public-Internet-only SSRF filter or a network firewall.

Clients ignore proxy/environment settings, do not load netrc credentials, and clear cookies
between hops. No Authorization headers, credentials or cookie jar are accepted by configuration.
There are up to four concurrent services per batch and two concurrent API probe operations.
Configure only trusted GET health/status routes, and keep the existing loopback API boundary.

| Health | Deterministic meaning |
| --- | --- |
| unknown | No saved observation |
| unreachable | No HTTP response, including timeout, DNS, connection or TLS failure |
| unhealthy | Main response differs from expected status or violates probe safety/size limits |
| degraded | Main response matches but health endpoint fails/mismatches, or main response is slow |
| healthy | Main and optional health response match expected status within the latency threshold |

An error after headers preserves the HTTP status already observed; a model never decides
whether a 503 happened. Latency includes the complete bounded endpoint operation.

## Evidence and history

Live questions are routed before document retrieval and model inference. The deterministic
answer labels **LIVE OBSERVATION**, reports its observation time and reason, and says
**INDEXED DOCUMENTATION** was not consulted to infer current health. Documents continue
through their existing grounded evidence pipeline for configuration/how-to questions.
Unknown or stale state is not fabricated. Observations older than five minutes are labelled
stale. Every observed item has an `L` label, observation ID, service, endpoint, time, state,
status and latency, plus optional health endpoint details. History questions expose up to ten
recent complete observations and explain that unobserved gaps say nothing about uptime.
An optional AI interpretation is separate, unverified advice, never authoritative health data.

SQLite retains at most 50 observations per service and 5,000 across the application, pruning
on every insert in the same transaction. Service deletion removes its history. Retention is
count-based, not a time-series system; low-volume observations can be old. Whole-database
backup/restore includes history. This additive table works with existing schema-version-1
databases and existing JSON inventory records; monitoring does not rebuild document evidence.

## Acceptance and limitations

Run `.venv/Scripts/python.exe -u scripts/live_service_acceptance.py` on Windows (or the
equivalent installed Python command). It starts/stops a disposable real Grimoire server,
uses real loopback TCP, and checks the API, CLI, saved evidence and dependency readiness.
[Recorded acceptance](monitoring-evidence/local.json) distinguishes real local requests from
unit tests. Models are deliberately unconfigured: `/health` is 200 and `/ready` is 503, so the
combined target is correctly degraded. The disposable UI was also checked in a real browser:
configuration save, Unknown before probing, Probe, Degraded, HTTP 200 and health HTTP 503.
[UI evidence](monitoring-evidence/services.png) shows the result.

Remaining scope: no authenticated endpoints, custom certificate authorities, cross-origin
redirects, JSON health semantics, continuous history, scheduled probes or uptime calculation.
Real HTTPS certificate negotiation against a homelab endpoint is not accepted yet; TLS/SNI
failure paths are covered by mocks. The deterministic planner supports common English
health/history questions; mixed documentation/live questions are not synthesized together.
Monitoring shares SQLite availability and inventory access with the rest of the application.
Linux and Docker acceptance remain intentionally deferred; neither runtime was provisioned.

The recommended next infrastructure integration is read-only DNS resolution inspection for
configured service hostnames, with similarly bounded requests and traceable observations.
