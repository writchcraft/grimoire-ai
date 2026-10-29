# Read-only DNS inspection

DNS uses configured inventory, without a second target list. For monitored services,
the monitor URL's hostname wins; otherwise the inventory `hostname` is used.
Literal IP targets produce an explicit not-applicable observation without a lookup.
Only configured targets are inspected. Reads and ordinary chat never refresh them.

```console
grimoire services dns Grafana
grimoire services dns --json
```

`services dns [name-or-id]` performs an inspection and saves the result. Without a
service argument it inspects at most 50 configured targets. A five-second cooldown
reuses a recent observation with its original ID and timestamp. The Services view
offers **Inspect DNS** separately from **Probe**. Reloading shows stored state.

The API adds `POST /api/services/{id}/dns` and `GET /api/services/{id}/dns`.
POST accepts no target override; GET returns latest, up to ten history samples and
`addresses_changed` (true/false/null). `GET /api/services?health=true` also includes
stored DNS state. There is no bulk DNS endpoint; CLI and explicit chat refreshes
use the same bounded inspection service.

Ask “What does Grafana resolve to?”, “Which monitored services have DNS failures?”,
“Does Grafana resolve to the same IP as before?” or “Why might Grafana be unreachable?”.
Chat returns saved observations, marking those over five minutes old as stale.
Explicit `probe:true` allows refresh: DNS questions inspect DNS only; reachability
questions may inspect HTTP and DNS. No background work is scheduled.

`services/dns.py` adapts the OS system resolver (`getaddrinfo`, AF_UNSPEC,
SOCK_STREAM, AI_CANONNAME). Results include sorted, deduplicated IPv4 and IPv6
addresses, typed A/AAAA address records, canonical name where returned, query name,
UTC completion timestamp, latency, applicability, truncation and safe error category.
These are **system name-resolution results**, potentially from hosts files, local
caches, search suffixes or DNS. A canonical name does not prove a wire CNAME record.
No recursive alias follow-up or reverse lookup is implemented.

States are application-derived: `resolved`, `unresolved`, `timeout`, `invalid`,
`partial` (truncated results) and `unknown` (temporary resolver failure, capacity
unavailable or not applicable). An absent IPv6 result does not imply AAAA failure;
the system resolver does not expose independent family query outcomes.

Safety bounds: ASCII DNS-label validation (punycode accepted), 253-character name
limit, 63-character label limit, three-second response deadline, four native workers
per process, no worker queue, 32 processed resolver tuples, 50-service batch cap and
five-second persisted cooldown per service. Workers retain capacity after timeout
or cancellation until the OS call ends; they are daemon threads so a stalled resolver
cannot hold CLI process exit. No result supplies a command, URL, connection or target
for another inspection. Invalid names never reach the resolver. Trailing dots are
preserved for absolute queries. OS search suffix behavior otherwise remains intact.

The application does not traverse CNAME chains, so an alias loop cannot produce
unbounded application queries. Wire retries, CNAME traversal limits and transient
allocations inside the OS resolver remain OS-controlled. The response deadline and
retained worker slots bound application exposure, but cannot cancel a native lookup
or promise a particular wire-query/hop count. There is no explicit DNS server,
raw CNAME chain, TTL, DNSSEC validation, authoritative NXDOMAIN distinction, PTR,
TXT, MX or SRV support. An unresolved OS result is not presented as authoritative
NXDOMAIN. Linux/Docker resolver behavior has not been accepted locally.

`dns_observations` is a separate SQLite table using the HTTP history pattern:
50 samples per service and 5,000 globally, pruned transactionally. Deleting a service
removes its DNS history. Whole-database backups include it. Retention is count-based;
old observations can remain indefinitely. Changes compare the two newest successful,
complete observations of the same hostname. Failures, partial sets and different
hostnames produce an unknown comparison. This is sample comparison, never a claim
of continuous monitoring, long-term stability or root cause.

DNS evidence uses `D` labels and `evidence_type: dns_observation`; HTTP evidence
uses `L` labels and `http_observation`; indexed documentation stays in `sources`.
Each DNS citation includes its observation ID, hostname, addresses, record types,
canonical name, status, timestamp and latency. `services/dns_live.py` constructs
the facts and sample comparisons. Optional AI commentary stays in `interpretation`.
HTTP/DNS correlation names both observations and times and explicitly declines to
establish causation. DNS results are never supplied to HTTP as approved addresses:
HTTP retains its independent URL validation, all-address checks and connection pinning.

No infrastructure modification, resolver configuration, hosts-file changes, commands,
SSH, Docker API, dynamic DNS, scheduler or autonomous actions are provided.

See [acceptance report](dns-report.md) for executed checks and limitations.
