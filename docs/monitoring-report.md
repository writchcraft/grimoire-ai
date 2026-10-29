# HTTP monitoring acceptance — 29 October 2026

Implemented and accepted locally on Windows. Linux and Docker remain deferred. Overlapping
edits were reconciled into one probe, evidence path and history table. Unrelated work was
preserved; no commit was created. See the [operating guide](service-monitoring.md).

| Requested item | Result |
| --- | --- |
| 1. Architecture | `services/probe.py` measures HTTP; `services/live.py` assembles deterministic evidence and optional separate AI commentary. Existing API, CLI, SQLite and inventory extended. No scheduler or infrastructure control. |
| 2. Service model | Optional `monitor`: URL, health URL, expected status, timeout, latency threshold, environment, mandatory TLS verification. Existing names, tags and notes retained. [Safe example](services.example.json). |
| 3. Probe | GET only; reachability, status, latency, final URL, redirect count, safe metadata, observation ID and UTC time; optional health endpoint. Bodies are discarded. |
| 4. Safety | Overall deadlines; three same-origin redirects; 64 KiB raw-body cap; validated/pinned DNS addresses; metadata/special-address blocks; verified HTTPS/SNI. No credentials, query strings, proxies or cookies. Local/private addresses intentionally supported. |
| 5. API | Inventory can include stored health; individual/bulk POST probes; GET health/history. Chat probes only with explicit `probe:true`. Ordinary reads never probe. |
| 6. CLI | `services add`, `list`, `probe [name-or-id]`, `status [name-or-id]`, JSON output, and deterministic health chat without model inference. |
| 7. UI | Monitoring form, manual individual/bulk probes, health states, main/health status and latency, explicit check time. Browser verified configuration, Unknown, Healthy, Degraded and streaming chat evidence. [Cards](monitoring-evidence/services.png), [chat](monitoring-evidence/chat.png). |
| 8. Evidence | LIVE OBSERVATION separate from INDEXED DOCUMENTATION; L-labels, observation IDs, endpoints, times, status, latency, health details and reasons. Optional AI interpretation cannot overwrite facts. Unknown/stale/history scope explicit. |
| 9. History | SQLite prunes transactionally to 50 observations per service and 5,000 globally. Deletion removes service history. Backup/restore retains it. History answers inspect up to ten samples without uptime claims. |
| 10. Real local acceptance | PASS at 19:13:59 UTC: disposable real Grimoire, `/health` 200/406 ms/healthy, `/ready` 503 with models intentionally unconfigured, combined target degraded. Real API, CLI, evidence and chat checked; server stopped. [JSON evidence](monitoring-evidence/local.json). Separate browser acceptance used port 7864. |
| 11. Tests added/total | 37 monitoring tests. Final backend: 426 passed in 34.89 seconds. Frontend: 5 passed. Combined: 431 passed. Real TCP acceptance is separate from tests. |
| 12. Existing reliability | All 389 prior backend tests and 5 prior frontend tests pass: evidence/grounding, factual/qualified evidence, retrieval/held-out regressions, offline, native/routing, deployment and backup. Ruff, Python formatting, JS syntax and whitespace checks pass. One existing Starlette/httpx deprecation warning remains. Real GGUF quality evaluations were not rerun. |
| 13. Limitations | Point-in-time only; no uptime, authentication, cross-origin redirects, custom CA configuration, JSON health semantics or scheduling. Real HTTPS target acceptance remains outstanding; TLS/SNI failures are mocked. GET safety assumes correctly implemented endpoints. Mixed live/document synthesis is not implemented. |
| 14. Next integration | Read-only DNS inspection for configured hostnames, with bounded resolution and traceable addresses/errors separate from HTTP reachability. |

Tests use local servers/mock transports, with no public-service dependency. Real acceptance
requested loopback only; external networking was not blocked at the OS level. No Linux/Docker
runtime search, installation or provisioning occurred. Windows sandbox restrictions blocked
asyncio socket-pair initialization, so runtime tests ran outside it.

A Windows virtualenv child-process cleanup failure occurred after one acceptance retry passed
its assertions. The runner now stops its own process tree and publishes PASS only after cleanup;
the final invocation exited successfully. Disposable preview processes were stopped.

The resolver's underlying OS worker may finish after async cancellation; probe results still
observe the configured deadline, but CLI event-loop shutdown can wait for that worker. Count
retention does not expire old samples by age; stale labels remain essential.
