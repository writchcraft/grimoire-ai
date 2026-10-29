# Windows Event Log inspection

Grimoire reads existing local Windows events. It cannot modify Event Logs, services, audit policy, permissions, the registry or firewall. It does not run commands, PowerShell, subscriptions or remediation.

## Environment and permissions

Run the backend directly on Windows with Python 3.12+. The implementation calls the native Windows Event Log API in `wevtapi.dll` through Python `ctypes`; no additional package is required. Windows 10/11 and Windows Server 2016+ are the intended environments. The underlying API is older, but other OS versions have not been acceptance-tested. This milestone was verified on the current Windows host; it is not a certification across Windows releases.

The supported channel inventory is **System, Application and Security**. `events channels` probes these supported channels; it does not enumerate every custom/operational channel registered in Event Viewer. System and Application normally work with the running user's existing rights. Security is opt-in for queries and often inaccessible. `Security log: unavailable (permission denied)` is a supported state. No Administrator requirement, elevation request, token privilege adjustment or permission change is implemented. Diagnostics warn about inaccessible channels without making them readiness requirements.

A Windows backend can serve its local events to its local web client. A Linux backend or Linux container cannot read the Windows host's logs. On other platforms the integration reports `not available on this platform`; the rest of the application starts normally. Remote Windows hosts, exported EVTX files and Linux journald are not supported.

## Read and resource bounds

The native adapter binds only `EvtQuery`, `EvtNext`, `EvtRender`, `EvtOpenPublisherMetadata`, `EvtFormatMessage` and `EvtClose`. Queries use a fixed channel allowlist and validated XPath predicates, newest records first. Every native event, metadata and query handle is closed, including error paths. No shell is involved.

Defaults are the latest **100 System records at warning severity or higher**. Queries always have a record limit; the hard maximum is 200. Omitting a time filter means the latest matching bounded records regardless of age, not a complete-log read. UI defaults to the last 24 hours. Chat defaults to the last 24 hours and at most 20 records per queried channel; “last hour” selects one hour.

Filters support channel, minimum native severity (`critical`, `error`, `warning`, `info`, `verbose`, or `all`), exact provider, event ID, inclusive start/end timestamps, and record ID. Severity uses native numeric levels, not text matching. Level 0/unrecognized levels stay `unknown`; use `all` to include them. Provider names containing XPath delimiters are rejected rather than interpolated as expressions.

Explicit times require an offset or `Z`. Start/end windows cannot exceed seven days. One omitted endpoint defaults to now or 24 hours before the end. `around` with `minutes` selects a symmetric window, default ±5 minutes, maximum ±60. Do not combine `around` with start/end. Native XPath timestamps use UTC `Z` syntax. Original source timestamp text is retained alongside normalized UTC timestamps and XML; Python normalized timestamps have microsecond precision while XML retains the native precision.

`before_record` provides older-record pagination within one channel. Preserve the original time/filter values across pages. Record IDs are channel-scoped, and pagination is not a frozen snapshot: logs can rotate or change while inspecting them. `next_before_record` is offered when a full page is returned; a subsequent empty page can mean the end.

Native XML and message buffers are capped at 64 KiB each, and returned serialized event data is capped at one MiB per read. Reads have a five-second application deadline, a 250 ms `EvtNext` wait and two process-wide read slots. These are bounded synchronous native reads, not a guarantee of preempting an OS API stalled inside Windows. Oversized/malformed events are omitted with explicit issues. Messages that cannot be rendered remain `null` with `message_status: unavailable`, while source XML remains available. No truncated message is presented as exact evidence.

Selected evidence is capped at 50 unique records, one MiB total, with a ten-second selection deadline checked between reads. Select fewer records if those bounds are reached. Incidents copy selected evidence into Grimoire's own local storage; ordinary event reads are not automatically indexed or archived.

## CLI

```powershell
grimoire events channels
grimoire events recent
grimoire events recent --channel Application --level all --limit 20
grimoire events search --event-id 7031 --provider "Service Control Manager" --json
grimoire events inspect 12345 --channel System --json
grimoire events recent --level error --start 2026-10-29T18:00:00Z --end 2026-10-29T19:00:00Z
grimoire events recent --around 2026-10-29T19:42:14Z --minutes 5
grimoire events recent --before-record 12345 --limit 100
grimoire events analyse --channel System --level error --limit 10 --question "Are these failures repeated?"
grimoire events analyse --channel System --limit 10 --use-model --json
grimoire doctor
```

`recent` and `search` share the same bounded filter implementation. `inspect` requires a channel because record IDs are not globally unique. Human output shows event facts; `--json` includes exact evidence/source fields. Do not pipe raw event output into remote services. `events analyse --service <inventory name or ID>` additionally compares retained service observations by timestamp.

## API

- `GET /api/events/channels`: supported channels and readable/unavailable states.
- `GET /api/events`: the bounded filters above, records, issues, effective query and optional `next_before_record`.
- `GET /api/events/{record_id}?channel=System`: one record; 404 if absent, or an explicit unavailable-channel result.
- `POST /api/events/analyse`: bounded `query` or explicit `selected` references, `question`, `notes`, `service`, `use_model`; optional `incident_title` saves through the existing local incident pipeline.

An event selection contains only server-verifiable references:

```json
{
  "selected": [
    {"channel": "System", "record_id": 12345, "evidence_id": "WE-<64 lowercase SHA-256 hex characters from the actual record>"}
  ],
  "question": "What happened around this service failure?",
  "service": "inventory-service-name-or-id",
  "use_model": false,
  "incident_title": "Windows service investigation",
  "notes": "Operator's report of the symptoms"
}
```

The reference above is illustrative; copy a real `evidence_id` from retrieval. Selected records are reread and their hashes checked. Rotation, access changes or a changed record cause 409; clients cannot submit their own event facts. Duplicate references are deduplicated. Request limits, no-store headers, origin protection and application request logging protections also apply.

The existing `POST /api/incidents/summarise` accepts `event_selection` in addition to supplied log content and notes. It combines the native event adapter with the existing log-analysis contract, preserving original line references. Explicit timezone events are ordered chronologically; naive or missing log timestamps follow in source order and are not assigned a guessed timezone.

## UI and questions

Open **Logs & incidents → Windows Events**. Choose the channel, minimum severity, exact provider, optional event ID and time window, then read a page. An optional local timestamp switches to ±5 minute filtering. Use **Next older records** for bounded paging. Each card shows native severity, provider, event ID, record ID, timestamp, rendered message and evidence identity. Expand **Exact source fields** to inspect structured data and XML.

Select up to 50 records, optionally choose a configured service for time comparison, and ask a question. **Explain selected events** separates Confirmed / Probable / Unknown. **Create incident from selected events** uses the incident title and notes in the existing form. The original **Create incident** action also includes selected Windows events alongside pasted logs. Service observations near selected events are retained separately in incident data and Markdown.

Knowledge-mode assistant questions mentioning Windows events/services, Event ID, system errors, critical events or errors on this PC use the native event branch. Examples: “What errors happened on this PC recently?”, “Summarise critical events from the last hour”, “What does Event ID 7031 mean in this incident?”, and “Were there system errors around the time the API went offline?”. Text questions use simple deterministic intent/filter rules; arbitrary natural-language incident timestamps and provider names are not parsed. Use the dedicated filters for exact windows. Missing matching evidence is reported, not replaced with documentation or invented events. Security is read in chat only when explicitly named.

## Evidence and interpretation

Every measured record has `evidence_type: windows_event` and a `WE-` SHA-256 identity over the exact returned source XML. Fields include channel, provider, event ID, native level/severity, normalized and source timestamps, computer, channel-scoped record ID, task, opcode, keywords, exact rendered message/status, named/unnamed EventData values and full bounded source XML. Optional or unavailable fields remain absent/null/unknown. UserData and other XML details remain available in the original XML even when not flattened.

Observed event rows, evidence IDs, counts and timelines are built by application code. Grouping uses exact channel + provider + event ID, retaining every member's identity and timestamp. Same event IDs from different providers/channels stay separate. A repeated group can contain multiple services or messages and does not by itself establish a crash loop, malicious login activity, disk failure or a common cause.

The adapter reuses `explain_logs`, `model_events`, interpretation candidates, validated line citations, uncertainty handling, incident rendering and incident storage. It does not introduce a second generative analysis engine. Optional AI can select only the existing validated symptom classes (connection failure, timeout, repeated restart, unknown). Broader Windows-specific root-cause interpretation remains limited; exact native messages and groups remain useful confirmed facts even when AI reports unknown. AI cannot return event fields or timeline entries through this contract.

A selected inventory service permits comparison with the latest retained 50 HTTP, 50 DNS and 50 TLS observations, using actual timestamps within ±5 minutes. Each result retains its observation type, ID, full observation, matched Windows evidence IDs and the time window. It explicitly states that proximity does not establish a host association or causation. No new HTTP/DNS/TLS probes are run by event analysis. No monitoring history beyond retained samples is inferred, and Grimoire does not yet map arbitrary Windows service names to inventory entries automatically.

## Privacy and locality

Events can contain usernames, hostnames, paths, IP addresses, SIDs and authentication metadata. The UI makes this visible before reading. Read only what is relevant and be careful sharing incident exports and backups: selected incident evidence is stored locally without additional encryption.

No event payload is logged in Grimoire's own application logs. Acceptance reports contain result metadata, not native messages or XML. Native exceptions are converted to generic channel status/code without copying their raw details. Ordinary reads do not enter vector storage.

Optional Windows-event model interpretation requires **in-process native inference for every candidate and fallback**. Remote adapters, unknown adapters and Ollama routes (including loopback daemons, which could themselves forward requests) are refused for this data with `local_model_required`; deterministic inspection remains available. No network/model cloud request is made by this integration.

The shared model selector excludes **all Security records** from model input. For System/Application it selects at most 12 rows and 5000 serialized characters, preserving whole rendered messages and using only reference metadata plus the message. Raw XML/structured payloads are not sent to the model. Operator questions and notes are bounded and treated as untrusted input. Host-event assistant replies are excluded from frontend conversation history so later unrelated model routes cannot inherit the event payloads.

## Limitations and verification

This is local, on-demand inspection of three channels, not Event Viewer or a background collector. No subscriptions, log clearing/writing, service control, commands, audit changes, registry changes or remediation exist. There is no journald support. Windows container/service accounts, custom channel ACLs, absent publisher resources and Event Log service failures can limit access or rendering. No privileges are adjusted. Only the current host was acceptance-tested, and no real service outage was induced.

Run deterministic fixtures with `python -m pytest backend/tests/test_windows_events.py`. Run all backend tests with `python -m pytest backend/tests`, frontend tests with `npm --prefix frontend test`, and local-model reliability with `python -m app.cli evaluate --suite all --output <report.json> --json`. With existing local weights configured, `python scripts/windows_events_acceptance.py` verifies real reads, filtering, identity, CLI, API, local AI and incident assembly without creating Windows events. It writes only acceptance result metadata. See [the milestone report](windows-events-report.md).

API references: [Windows Event Log API](https://learn.microsoft.com/en-us/windows/win32/api/winevt/index), [EvtFormatMessage](https://learn.microsoft.com/en-us/windows/win32/api/winevt/nf-winevt-evtformatmessage).
