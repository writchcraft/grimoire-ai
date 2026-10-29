# DNS inspection acceptance — 29 October 2026

Implemented in the existing working tree, preserving prior milestone work. No commit,
push, infrastructure change or control capability was added. See the
[operating guide](dns-inspection.md).

| Requested item | Result |
| --- | --- |
| 1. Architecture | Dedicated `services/dns.py` system-resolver adapter; deterministic observation data and `dns_live.py` evidence assembly. No model determines resolution outcomes. |
| 2. Inventory | Monitor URL hostname is authoritative; inventory `hostname` is reused when no HTTP monitor exists. IP literals explicitly skip lookup. No duplicate configuration. |
| 3. Record types | A/IPv4 and AAAA/IPv6, sorted/deduplicated and typed; final canonical-name hint where the OS supplies it. This is not raw wire CNAME inspection. |
| 4. Safety | Strict label validation; three-second result deadline; four retained native worker slots, no queue; five-second persisted cooldown; 32 processed tuples and 50-service batches. No application alias recursion, reverse lookups, connections, arbitrary-target API or response-driven actions. HTTP SSRF validation and address pinning are unchanged. |
| 5. API | POST and GET `/api/services/{id}/dns`; stored DNS attached to inventory with `health=true`. Reads do not query. GET includes a ten-sample history and address-change comparison. |
| 6. CLI | `services dns [name-or-id]`, batch mode and `--json`; hostname, state, A/AAAA, canonical name, latency, reason and timestamp. No model loading required. |
| 7. UI | Separate Inspect DNS control, resolved/unresolved/unknown state, addresses, canonical name, timestamp, stale/truncated/change labels. Browser verified both localhost resolution and a hostname-only invalid target, including persistence after reload. [Screenshot](dns-evidence/services.png). |
| 8. Evidence | Typed `dns_observation`, D-label, observation ID, hostname, typed addresses, record types, timestamp, status, optional canonical name and recent history. HTTP is explicitly `http_observation` with L-labels; indexed documents remain separate. Facts are application-assembled; optional model interpretation remains a separate field. |
| 9. History | Separate SQLite DNS table: 50 per service, 5,000 globally, transactional pruning and service-deletion cleanup. Two newest samples compare only when both are resolved, complete, applicable and for the same hostname. No uptime/stability claims. |
| 10. HTTP/DNS correlation | Both observation IDs/timestamps are cited; health and resolution remain independent. The answer explicitly avoids causation and notes cache/time differences. DNS-only refresh does not issue an HTTP request. |
| 11. Real local acceptance | PASS using the real Windows system resolver: localhost returns 127.0.0.1 and ::1; reserved `grimoire-dns-acceptance.invalid.` returns unresolved. Disposable configured localhost service passes DNS/API/CLI/evidence/chat acceptance; literal loopback HTTP succeeds. Existing localhost HTTP safety refusal is retained and recorded. No configured LAN hostname existed, so LAN acceptance is explicitly untested. [JSON](dns-evidence/local.json). |
| 12. Tests | 28 new backend DNS cases and one frontend test. Final full gate: **454 backend + 6 frontend = 460 passed**. Existing 426 backend, including all 37 HTTP monitoring tests, and five frontend tests remain passing. Ruff, Python formatting, JS syntax, whitespace and tracked-file hygiene pass. One existing Starlette/httpx deprecation warning remains. |
| 13. Reliability | Real Qwen2.5-1.5B Q4_K_M + MiniLM CPU development and held-out suites rerun unchanged: development supported/oracle 10/10 each, refusals 4/4 each, citations 18/18, logs/extraction/incidents 6/6 each; held-out supported/oracle 22/22 each, refusals 5/5 each, citations 25/25, logs/extraction/incidents 7/7 each. Command exited 0. [Full report](dns-evidence/reliability.json). These are previously exposed regression suites, not a new blind assessment. |
| 14. Limitations | OS resolver/cache/hosts semantics; no independent per-family query outcome, TTL, raw CNAME chain, authoritative NXDOMAIN, DNSSEC, custom resolver or additional record types. OS owns wire retries/hop counts and native transient allocation; deadlines cannot cancel a native lookup. Timed-out calls retain slots and cannot block CLI exit. Count retention is not age expiration. No real LAN, Linux/Docker or standalone DNS-interpretation model-quality acceptance. |
| 15. Next read-only integration | TLS certificate inspection for configured HTTPS endpoints: validity window, hostname match, issuer and fingerprint as separate measured evidence, reusing HTTP target protections. No certificate renewal, issuance or server changes. |

Real local acceptance uses `scripts/dns_acceptance.py`, disposable SQLite state,
a loopback HTTP server and in-process FastAPI requests. CLI execution exercises the
real parser/command handler. Automated DNS tests use resolver fixtures and do not
depend on public DNS. Local acceptance uses no public internet names, but OS networking
was not disabled. The configured database was inspected read-only and contained no
services. No LAN address or resolver configuration was invented or changed.

The real localhost test demonstrates why DNS inspection and HTTP policy stay separate:
the OS returns ::1, which the current HTTP reserved-address policy refuses. The DNS
observation remains resolved while HTTP records `blocked_target`; a separate
127.0.0.1 HTTP target is healthy. That existing policy was deliberately preserved.

Real evaluation retrieval hit@1/@3/@5: development 8/10, 10/10, 10/10;
held-out 18/22, 21/22, 22/22. Supported/oracle/refusal/log/citation checks all pass;
these narrow tests do not establish general model accuracy or long-term DNS stability.
Frozen suites, expected evidence and scorers were not edited.

Windows sandbox socket-pair initialization stalled one initial pytest attempt before
test execution. It was stopped; completed runtime tests and acceptance ran outside
that sandbox. Preview servers created for browser checks were stopped afterward.
