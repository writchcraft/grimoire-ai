# Read-only Windows host inspections

Grimoire inspects the Windows computer running its backend. It does not inspect the browser's
computer when the browser is elsewhere. Run as an ordinary Windows user. No Administrator
prompt, shell, PowerShell, service control, remote host session or automatic remediation exists
in this feature. Unsupported platforms report unavailable; a Linux provider is not implemented.

## Usage

```text
grimoire host status
grimoire host system --json
grimoire host resources --json
grimoire host disks
grimoire host network
grimoire host services --state stopped --limit 20
grimoire host services --name sql --start-type automatic
grimoire host services EventLog --json
grimoire host history --json
```

Service positional arguments inspect an exact case-insensitive service name. `--name` is a
case-insensitive substring filter on the service name, not the display name. States include
running, stopped, paused and the Windows pending states. Start types are automatic, manual and
disabled. The default service limit is 100; the maximum is 200. Missing optional fields are null
and accompanied by issues. A stopped service is not automatically considered faulty: manual,
trigger-start and disabled services are commonly stopped by design.

GET endpoints: `/api/host`, `/api/host/system`, `/api/host/resources`, `/api/host/disks`,
`/api/host/network`, `/api/host/services`, `/api/host/services/{name}`,
`/api/host/history?limit=20` and `/api/host/correlations?service=<inventory-name-or-id>`.
Service list query parameters are `name`, `state`, `start_type`, `limit`. Invalid filters return
422; an absent exact service returns 404 when the read succeeded. No host mutation routes exist.

The **This host** page provides explicit inspection buttons, compact resource cards, expandable
disks and interfaces, filtered services and a separate supporting-evidence view. There is no
polling loop. `grimoire doctor` reports host/system, disk, network and service readability;
optional unavailable fields produce warnings rather than application failure.

## Evidence and thresholds

`HostProvider` defines the provider contract. `WindowsHostProvider` implements it with allowlisted
psutil 7.x getters and standard-library Windows metadata reads. See the
[psutil API documentation](https://psutil.readthedocs.io/stable/index.html) for the getter semantics.
CPU utilisation is measured over 0.1 seconds; other facts are acquired sequentially during the
same inspection, not atomically. Disk utilisation means space used, not I/O activity.

Each `host_observation` or `windows_service_observation` contains a hostname, UTC observation
timestamp, section/service identifier, measured facts and a SHA-256 evidence ID. Classification
happens before hashing. The hash binds the collected record; it is not an authenticity signature.
The application never asks a model to produce or edit these facts. Host data is never passed to
model prompts, embeddings or outbound inventory transport. The API/UI return it to the local
application's authorized operator through the existing deployment boundary.

Deterministic defaults, configurable through normal Grimoire settings:

| Environment setting | Default | Meaning |
| --- | --- | --- |
| `GRIMOIRE_HOST_DISK_USED_PERCENT` | 95 | Flag low space at or above this percentage used |
| `GRIMOIRE_HOST_DISK_FREE_GIB` | 2 | Flag low space at or below this much free space |
| `GRIMOIRE_HOST_MEMORY_USED_PERCENT` | 90 | Flag high memory utilisation at or above this percentage |

Disk conditions use OR. Labels describe thresholds, not incident cause or LLM-defined severity.
Low space/memory can yield a separately labelled possible contribution, with an explicit unknown
cause. Uptime at most 600 seconds is reported as a measured recent-boot observation. No sparse
sample is presented as an uptime trend or availability percentage.

There are at most two concurrent native inspection calls. Service enumeration has a five-second
cooperative budget and a 4096-name scan cap. Volume results cap at 64, interfaces at 128 and IP
addresses at 32 per interface. Truncation and unavailable fields are reported. Native OS calls
themselves are not forcibly interrupted. Fixed local volumes only are probed; mapped network
drives and removable media are excluded.

API and CLI inspections retain at most 50 inspection pages total in SQLite's `host_inspections`
table. The oldest pages are pruned transactionally, including service pages. This bounded local
history preserves original evidence for explicit observations; it does not continuously sample.
History reads and doctor do not create new history. SQLite snapshots include the table.

## Explicit correlation and incidents

Set `local_windows_service` on a Grimoire inventory service, either through the Add service form
or service JSON. This means **the service on this backend machine**; the mapping is an operator
assertion, not an inferred deployment relationship. Similar names never create mappings.

```json
{
  "name": "Example API",
  "kind": "other",
  "local_windows_service": "ExampleAPI",
  "monitor": {"url": "http://127.0.0.1:8080"}
}
```

The correlation endpoint makes a current host inspection, optionally reads the mapped Windows
service and compares its state with the latest retained HTTP observation. The HTTP observation
keeps its own timestamp and may be old; it is never relabelled current or reprobed implicitly.
An unreachable HTTP observation plus a stopped mapped service supports only that conjunction.
It does not prove causation. Investigate timestamps and mapping validity before drawing conclusions.

Windows events require an exact case-insensitive hostname match (no guessed FQDN aliases).
Stopped-service support requires an exact structured event-data service/display-name match,
SCM provider, event ID 7031/7034/7036 and a ±5-minute observation window. Startup support requires
EventLog/6005 or Microsoft-Windows-Kernel-General/12 within ±5 minutes of the measured boot.
All links retain both evidence IDs. Bounded recent System samples can miss useful events; exact
event queries around a boot time can supply them to the correlation helper.

To attach host evidence to a supplied-log incident, select **Attach current host evidence** or
set `include_host: true` on `/api/incidents/summarise`. Verified selected Windows events are also
compared when present. Host context is appended after model analysis, kept separate in the
incident and included in its local export. It is never sent to the model.

No usernames, service account names, binary paths, process command lines, profile paths, license
keys, environment dumps, MAC addresses or credentials are retained. CPU model uses one read-only
hardware metadata registry value; there is no registry editing. Gateway/DNS configuration is not
collected. Hostnames, service names and assigned IPs are operational identifiers: local SQLite,
incidents, exports and backups containing them should remain private. No additional at-rest
encryption is introduced.

Run real non-Administrator acceptance with `PYTHONPATH=backend` and
`python scripts/host_acceptance.py`. It saves sanitized metadata, verifies real Windows APIs,
and uses an unused loopback port for an unreachable HTTP example without stopping any service.
