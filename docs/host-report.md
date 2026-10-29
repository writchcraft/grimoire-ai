# Windows host inventory milestone report

1. **Architecture.** Dedicated `app.services.host` module with a conceptual `HostProvider`
   protocol, `WindowsHostProvider`, shared evidence/threshold/assessment/correlation functions,
   local bounded history and a separate API router. No Linux implementation. Earlier uncommitted
   repository work was preserved.

2. **Libraries and APIs.** Added `psutil>=7.2,<8`; real acceptance used 7.2.2. Allowlisted system,
   memory, volume, interface and Windows service getters wrap native Windows queries. CPU sampling
   uses psutil's system CPU times; services use native SCM enumeration/status/configuration reads.
   Standard-library `platform`, `sys.getwindowsversion`, `socket.gethostname` and read-only
   `winreg.KEY_READ` hardware CPU metadata complete OS identity. No subprocess or shell integration.
   Getter behavior is documented in the [official psutil reference](https://psutil.readthedocs.io/stable/index.html).

3. **OS facts.** Hostname, Windows name/release/version/build/edition, architecture, UTC boot time
   and elapsed uptime seconds. Boot time includes the provider's suspend/hibernate semantics;
   samples are sequential, not an atomic machine snapshot.

4. **CPU and memory.** CPU model, logical processor count, actual 0.1-second CPU utilisation,
   total/available/used memory bytes and memory percentage. Missing fields are null with issues.

5. **Disks.** Fixed local volume identifier, filesystem, total/free/used bytes, used percentage
   and deterministic low-space state. This is capacity utilisation, not disk I/O telemetry.

6. **Network.** Interface identifier, up/down state when available, IPv4/IPv6 addresses and
   loopback indication. No adapter changes, MAC collection, Wi-Fi secrets, route or firewall access.
   Gateway/DNS configuration is deliberately omitted.

7. **Services.** Native service name, display name, state, start type and PID where available.
   Accounts and command lines are omitted. Exact and substring-name inspection, state/start-type
   filtering, 100 default/200 maximum results. Exact inspection falls back to enumeration if its
   configuration read is denied, preserving readable status fields.

8. **Privileges.** Real acceptance asserted `IsUserAnAdmin() == False`. There is no application
   escalation path. Optional denied fields remain unavailable; other reads continue. The agent's
   restricted execution sandbox denied SCM access and blocked loopback test clients; running tests
   outside that sandbox did not elevate the Windows account. The browser displayed a real denied
   start-type field while retaining the service's other facts.

9. **Evidence.** Distinct `host_observation`/`windows_service_observation` records retain host,
   UTC observation timestamp, identifier, facts and SHA-256 evidence ID. Threshold labels are
   application-owned. Models cannot supply facts, edit measurements, redefine severity or receive
   collected inventory. Host evidence stays outside document retrieval and model conversations.
   SQLite retains at most 50 explicit inspection pages total; no continuous sampler or trend claim.

10. **CLI.** Added `host status/system/resources/disks/network/services/history`; positional exact
    service lookup and bounded filters; `--json` throughout. Concise human status shows OS, uptime,
    CPU, memory, disk space and interfaces. Doctor adds four host-readability checks.

11. **API.** Read-only host/system/resources/disks/network/services/detail/history/correlation GET
    endpoints. Invalid service filters return 422; missing exact services return 404 when readable.
    `include_host` optionally adds deterministic host context to existing incident creation.

12. **UI.** Added a dedicated This host view with explicit inspection buttons, resource cards,
    compact expandable disks/interfaces, bounded filtered services, timestamp/evidence availability
    and a separate concise Confirmed/Probable/Unknown supporting-evidence view. Added explicit local
    Windows service mapping to the service form and an incident host-evidence checkbox. No polling.
    Browser acceptance inspected real resources, disks/interfaces and stopped services; measured
    fields and denied optional fields were visible.

13. **Windows Event correlation.** Exact host/service identifiers, known SCM/startup providers and
    IDs, structured data matches and bounded time windows; no message-name guessing. Real acceptance
    found two existing startup records matching measured boot time. Startup support says the event
    is consistent with that boot time, avoiding an unsupported claim that an older boot is recent.
    No events were generated or logs changed. A recent broad sample can omit startup records.

14. **HTTP/service correlation.** Optional operator-authored `local_windows_service` mapping reads
    the named local service and joins its facts to the latest saved HTTP observation. Both times
    and evidence IDs remain separate. Real acceptance probed a test-owned unused loopback port and
    read an already-stopped mapped Windows service. The conjunction was confirmed without stopping
    any service or inducing a production outage; cause remains unknown.

15. **Real Windows acceptance.** Windows 11 Professional build 26200, AMD64, 16 logical processors,
    four fixed local volumes, four interfaces. OS/boot/uptime/CPU/memory/space/interfaces, a bounded
    200-service page, running/stopped filters, exact lookup, evidence, CLI JSON, API and browser UI
    passed. The acceptance artifact omits hostname, addresses, service names and raw event payloads.
    See [acceptance metadata](host-evidence/acceptance.json) and
    [reproducible script](../scripts/host_acceptance.py).

16. **Privacy and safety.** No host control, process termination, registry writes, scheduled tasks,
    adapter/DNS/firewall modifications, shell, PowerShell, remediation or outbound inventory
    transport. No inventory reaches model prompts or embeddings. Service accounts, executable
    paths, usernames, license keys, process arguments and environment secrets are excluded. Local
    history/incidents/backups remain sensitive and rely on the existing deployment access boundary.

17. **Tests.** Added 22 deterministic backend host tests and three frontend tests: metadata, uptime,
    resources, disk thresholds, interfaces, service states/filters/exact lookup, missing/denied
    fields, exact-query fallback, bounds/truncation, hashing, history pruning, API/CLI/JSON,
    correlation exclusion rules and incident prompt privacy. Final results: 542 backend tests passed and 13 frontend tests passed.
    See [backend results](host-evidence/backend-tests.txt) and [frontend results](host-evidence/frontend-tests.txt). Existing Starlette
    test-client deprecation warning remains. Ruff, frontend syntax and whitespace checks passed.

18. **Reliability.** Both frozen suites ran with the existing local Qwen2.5 1.5B Q4_K_M generation
    and MiniLM embedding models. Suite hashes match the preceding Windows Event milestone.
    Development: grounded/oracle QA 10/10, unsupported/oracle unsupported 4/4, logs/extraction/
    incidents 6/6 each, hit@5 10/10, 18/18 traceable validated claims, zero rejected outputs.
    Held-out: grounded/oracle QA 22/22, unsupported/oracle unsupported 5/5, logs/extraction/incidents
    7/7 each, hit@5 22/22, 25/25 traceable validated claims, zero rejected outputs.
    [Summary](host-evidence/reliability-summary.json), [full synthetic results](host-evidence/reliability.json).

19. **Limitations.** Windows local backend only; no remote inventory, Linux, complete device
    inventory, per-process data, service accounts, delayed/trigger-start detail, gateways/DNS
    settings, removable/network volume inspection or continuous telemetry. CPU is a short sample.
    Native calls cannot be forcibly interrupted. Up to two native inspections, five-second/4096-name
    service scan budget, 64 volumes, 128 interfaces and 32 IPs/interface; truncation is explicit.
    Exact hostname correlation deliberately excludes unconfigured FQDN aliases. Mapping is an
    operator assertion. Sparse history proves sampled states only. Hashes bind evidence content,
    not authenticity. One real Windows machine was tested; no production outage was induced.

20. **Recommended next integration.** Add a small operator-configured application-health evidence
    adapter that joins existing HTTP health checks to these explicit local service mappings. Keep
    the same bounded observation/evidence contract and measure whether it improves incident
    explanations before expanding telemetry or scheduling.

Operational reference: [host inspection documentation](host-inspection.md).
