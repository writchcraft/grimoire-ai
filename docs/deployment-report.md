# Deployment reliability report — 29 October 2026

**The milestone is CLOSED for the current Windows development environment.** Windows native development/runtime is **VERIFIED to the extent documented**. Linux native deployment and Docker deployment are intentionally **DEFERRED / NOT YET TESTED** as future release/deployment tasks. They do not block continued Grimoire development or a supervised first read-only homelab integration on Windows. Deployment recovery has executed with real models on Windows. No new models, routing policy, integration, telemetry or cloud service was added. Existing working-tree changes from the model milestone were preserved.

## 1. Linux environment tested

None. The available host is **Windows 11 build 26200, Python 3.12.3**. `wsl --list --verbose` reports WSL is not installed; the Docker executable is absent. Ubuntu Server 24.04 LTS / Python 3.12 is the documented target, not an executed environment. Installing Linux, package installation there, native binding loading there, systemd, filesystem permissions there and Linux frontend/backend networking remain **DEFERRED / NOT YET TESTED**.

## 2. Filesystem/storage architecture

One configurable data directory contains SQLite and its active WAL/SHM files. SQLite is authoritative for document metadata, chunk text/vectors, FTS, service inventory, newly retained original upload bytes and newly persisted incident Markdown/analysis. Qdrant rebuilds in memory. Models, configuration, code, stdout/stderr logs, provisioning caches and backups are separate. No mutable per-document paths are scattered through the checkout. Development retains `data/runtime`; production uses an explicit absolute path or the Compose named volume.

## 3. Restart persistence

**PASS on Windows.** The real Qwen2.5-1.5B Q4_K_M + MiniLM F16 lifecycle rehearsal starts the production ASGI app, ingests a known Grafana document, saves inventory and an incident, stops, restarts, and returns the same grounded answer with identical document/chunk IDs, excerpts and line ranges. Fresh runtime objects reconstruct the vector index. Frontend HTML/JavaScript and API requests use the same application origin.

## 4. Unclean restart

**PASS on Windows, disposable state.** A separate rehearsal starts the actual `grimoire serve` CLI over real loopback TCP, ingests data, performs RAG, forcibly kills that server process, starts another server and verifies documents, inventory, incidents, lexical search and identical cited RAG. Separate subprocess tests bypass cleanup using `os._exit`: committed service writes survive; uncommitted writes disappear. Ingestion crash tests exit after Qdrant upsert but before SQL commit, or immediately after commit. On restart, document/original/vector/FTS visibility agrees with the transaction outcome and foreign-key checks pass. No real user data was corrupted. Power-loss and Linux SIGKILL/filesystem behaviour remain untested.

## 5. Graceful shutdown

**PASS for production application lifecycles on Windows.** Three real-model start/stop cycles close generation, embedding and vector-store handles; Python thread count returns from 5 to 1 each time. RSS returns near the pre-load range. Existing cancellation tests cover draining in-flight native calls and closing streams. Actual Windows TCP-server termination is abrupt and does not certify signal-driven graceful shutdown. Linux SIGTERM, systemd and Compose stop behaviour remain unexecuted. The Compose/example systemd stop timeout is two minutes; slow hardware may need more.

## 6. Resource measurements

Real CPU inference, no GPU layers, generation context 4096, embedding context 512, output limit 384, two configured native threads. Both llama.cpp generation and batch thread settings now honor that bound. The final rehearsal ran after the separate reliability evaluation finished. These are **Windows process snapshots**, not Linux sizing guarantees, continuous peaks, throughput benchmarks or enforced memory limits.

| Phase | Observed resident memory |
| --- | --- |
| Before application startup | 100.2 MiB |
| Embeddings loaded, generation lazy | 149.0–157.2 MiB |
| After first embedding/ingestion | 173.2 MiB |
| Both models loaded, RAG/search complete | 1932.1–1935.4 MiB |
| Loaded idle | 1932.2–1935.4 MiB |
| After shutdown | 119.1–120.5 MiB |

The short RAG request, including cold generation loading in each cycle, took 9.27–9.99 seconds wall time and 16.39–17.63 seconds process CPU time. Loaded idle CPU over two-second windows was 1.56–3.12% of one core; short sampling is noisy. Some post-shutdown allocator retention remains, with a small rise over three cycles; this is not a long-duration leak/soak result. Large corpora, concurrent embedding/generation, low-memory/OOM recovery and Linux cgroup enforcement remain unmeasured. An optional Compose example supplies two CPUs and 4 GiB backend memory; **the limits have not executed** and are absent from defaults.

## 7. Disk observations

| Consumer | Observed / policy |
| --- | --- |
| Preferred generation GGUF | 1,117,320,736 bytes |
| MiniLM embedding GGUF | 45,949,216 bytes |
| Optional existing 3B comparison GGUF | 2,104,932,768 bytes, not loaded by this rehearsal |
| Restored single-document database | 90,112 bytes, includes metadata, original, chunk/vector, inventory and incident |
| Vector index | in memory; persisted vector arrays are within SQLite |
| Uploaded document | 34 bytes in this fixture; original stored inside SQLite, not an extra file |
| Application log files | none generated by normal native runtime; stderr is operator-managed |
| Compose logs | configured 10 MB × 3 files per frontend/backend container; unexecuted |
| Model caches | none created by native runtime; provisioning/pip/build caches excluded |
| Backups | complete database snapshots plus a small manifest; retention is operator-managed |

There is no total-corpus quota. Per-request byte/chunk bounds remain, incidents have bounded listing plus deletion, and document deletion is supported. Deleted database pages may require offline VACUUM to return disk space. Monitor free space and prune data/backups deliberately; large-index growth is unmeasured.

## 8. Health/readiness and logging

`/health` remains a cheap liveness endpoint. New `/ready` checks generation state, embedding state and a read-only SQLite query with a short lock timeout, returning 200/503. It never loads models, runs inference or scans the index. Generation remains lazy; readiness is 503 until generation loads, so Compose's UI dependency uses liveness. Storage readiness checks readability, not future capacity/writability. `/api/status` remains richer diagnostics.

Application logs remain JSON on stderr with request IDs and route templates; configuration secrets, request bodies, document contents and raw provider exceptions are not serialized. Native readiness events now identify the embedding/generation role, and successful lifecycle closure logs `application_stopped`. Standard Uvicorn messages may also appear on stderr. Compose log rotation bounds ordinary container log growth.

## 9. Backup design

`grimoire backup create <directory> [--json]` uses SQLite's online backup API, including committed WAL data. It snapshots only authoritative data, without loading models. A 60-second progress deadline prevents an indefinitely busy snapshot. The manifest includes Grimoire version, UTC timestamp, backup/schema versions, component counts, embedding identity, vector backend/dimension and database SHA-256. Model weights, configuration/secrets, logs and caches are excluded. Data itself may contain sensitive operator content and must be protected. Private staging directories and owner-readable files precede atomic publication; POSIX file/directory flushing is implemented but not executed on Linux here.

## 10. Restore result

**PASS on Windows with actual models.** `grimoire backup restore <backup> <new-directory> [--json]` verifies checksums, integrity, foreign keys, vector consistency, supported schema and matching manifest before publication. It refuses existing destinations. The rehearsal creates known state, snapshots it, moves the original active directory aside, restores into a previously nonexistent directory, starts with that directory and verifies documents, inventory, incidents, vector retrieval and identical cited RAG. Tests separately cover a live WAL snapshot, original-byte recovery, tampering, existing-destination refusal and staging cleanup on simulated disk failure. Linux/container restore remains unexecuted.

## 11. Compatibility checks

Schema 0 upgrades additively to schema 1. Future/unknown schemas, malformed/nonfinite vectors and mixed dimensions fail clearly. Native embedding identity is now full-content SHA-256 plus the configured identifier, so unchanged files survive path/host/mtime changes. Changed weights or model identity refuse populated state; ingestion/query dimension changes fail. **Old native fingerprint indexes need re-ingestion into new storage**; keep original inputs and old-version backups. Do not edit identity metadata to bypass checks. Existing Ollama model-name compatibility is preserved, with revision pinning still the operator's responsibility. Application version and schema version are recorded separately; no general migration framework was added.

## 12. Docker result

**DEFERRED / NOT YET TESTED by project decision.** No Docker CLI is installed. No build, Compose configuration command, container startup, health check, mount, network, CPU inference, restart, backup/restore or stop acceptance is claimed. YAML received static parsing/review only. Compose now has explicit persistent state, read-only existing model mounts, a read-only backend root, bounded temporary storage/logging, init, restart policy, a stop grace period, configurable hosts/data path, and no backend published port. The optional resource override is a reviewed example. A Docker host must execute the guide before Docker deployment can be verified; this future gate does not reopen the closed Windows milestone.

## 13. Offline deployment result

**PASS at Python-process scope on Windows.** The real-model lifecycle rehearsal denies non-loopback Python socket connections and external DNS; no denied attempt was recorded through startup, assets, embeddings, generation, ingestion, retrieval, deterministic log/incident analysis, inventory, restart and restore. It is not OS network isolation and does not cover native-library network syscalls or a disconnected Linux/container deployment. The separate real TCP server rehearsal allows networking and makes no offline claim. No runtime downloading, telemetry or cloud fallback was added.

## 14. Security/permissions

The application requires no root; Docker uses UID/GID 10001 and models mount read-only. New data/backup directories request mode 0700, backup files 0600. Linux mode enforcement and systemd restrictions are reviewed, not executed. Upload filenames/source labels never become server write paths; originals/incident records are transactional SQL data. Backup paths reject symlink traversal and active-data nesting; restore refuses overwrite. Use trusted parent directories and trusted backups: checksums are not authentication, and local administrative filesystem races are outside this boundary. No application shell/SSH execution was introduced. Validation scripts launch only the known local Grimoire CLI without a shell. The API remains single-user/unauthenticated and loopback by default.

## 15. CLI changes

Added focused `backup create` and `backup restore` commands using the existing storage boundary. `doctor`, `status`, `models list` and `validate` executed with exit 0 against disposable known state. Their diagnostic limitations remain: `status` is a snapshot; synthetic `validate` is not a Linux/Docker certificate. No deployment mode was added that could mislabel unexecuted checks as passing. The two opt-in acceptance scripts report actual platform and test scope.

## 16. Documentation changes

[Deployment guide](deployment.md): Linux-first Compose/native setup, directory layout, ownership, health, logs, shutdown, restore, compatibility, offline testing and resource/disk observations. README, setup, architecture and validation now link current deployment evidence and explain the new identity/backup contract. [Optional systemd unit](../deploy/grimoire.service) and [optional Compose resource example](../deploy/compose.resources.example.yml) are clearly unexecuted; Compose remains primary.

## 17. Automated results

* Full publication gate: **389 backend tests, 5 frontend tests passed**; Ruff lint/format, JavaScript syntax, tracked-file artifact/credential-pattern audit and whitespace checks passed. One pre-existing Starlette/HTTPX deprecation warning remains.
* Deployment tests: **16 passed**, including live WAL backup, restore, manifest rejection, compatibility, process crash recovery, HTTP restart/citations, incident retention, readiness and CLI round-trip. Tests need sandbox escalation on this Windows host because restricted asyncio socket-pair initialization hangs before app startup.
* Real-model deployment lifecycle and real TCP process rehearsals: **passed**, with scopes above.
* Development reliability: supported and oracle answers **10/10**, unsupported refusals **4/4**, logs/extraction/incidents **6/6** each; retrieval top-5 **10/10**; traceable citations **18/18**.
* Held-out reliability: supported and oracle answers **22/22**, unsupported refusals **5/5**, logs/extraction/incidents **7/7** each; retrieval top-5 **22/22**; traceable citations **25/25**. Combined evaluation exit 0. These remain narrow synthetic contract checks, not broad real-world quality certification.

Machine-readable evidence: [native lifecycle/resources](deployment-evidence/windows-native.json), [real TCP server](deployment-evidence/windows-service.json), [AI reliability summaries](deployment-evidence/reliability-summary.json). These contain only disposable example data and local model identities. The final real-model restore rehearsal and all 389 backend tests passed after the backup deadline and read-only readiness refinements.

## 18. Remaining deployment limitations

Linux install/native loading and the complete documented Docker route are intentionally deferred and not yet tested. Linux restart/SIGTERM, non-root permissions, read-only filesystems, systemd, cgroup caps/OOM, OS-isolated offline operation and container backup/restore require target-host execution. The short resource rehearsal is not a capacity or long-term leak benchmark. SQLite/Qdrant requires one index-owning process; stop the server before separate CLI writers. No automatic migration/re-embedding, total disk quota, backup scheduler or backup encryption was introduced. Legacy originals cannot be recovered from old chunks. Initial provisioning still requires dependencies/images/weights to be acquired or transferred.

## 19. First read-only homelab integration readiness

**Ready to proceed with a supervised first read-only homelab integration on the existing Windows-native runtime.** This is a limited integration trial, supported by the recorded real-model CLI/browser workflows, restart/forced-stop recovery, backup/restore, persistence and development/held-out reliability results. It is not broad production certification or evidence that any real integration has already executed. No integration has been added or contacted in this milestone.

Use operator-selected copies/exports of runbooks and logs, keep Grimoire on its existing loopback/single-user boundary, retain source originals and a tested backup, and inspect cited excerpts and generated hypotheses before acting. Continue using one index-owning process. The small fictional reliability suites do not establish accuracy on real homelab material; check that during the first integration. OS-isolated offline operation, long-running soak/load behavior and capacity remain unverified. Linux/Docker acceptance is a future deployment task, required before sign-off on those platforms, with configuration and procedures preserved.

## 20. Linux/Docker acceptance follow-up — 29 October 2026

Historical attempt, superseded by the intentional deferral in section 23. Its runtime observations remain valid evidence; its blocker/sign-off language is not the current project decision.

**BLOCKED at the runtime prerequisite.** A fresh read-only inventory of this Windows host found no available Linux execution environment. Acceptance stopped here as requested; no OS features, distribution, virtualization software or Docker packages were installed. No product code, deployment configuration, models, tests or evaluation fixtures were changed during this follow-up.

Runtime discovery, in preference order:

| Check | Observed result |
| --- | --- |
| Existing WSL2 | Both `wsl --status` and `wsl --list --verbose` exit 1 and report: "The Windows Subsystem for Linux is not installed." The system `wsl.exe` launcher exists; it is not an installed Linux runtime. |
| Existing Linux VM | No `Get-VM`, `VBoxManage`, `vmrun`, `qemu-system-x86_64`, `multipass`, `vagrant` or `virsh` command found. No matching VMware, VirtualBox, QEMU or Multipass installation found in checked standard installation locations/uninstall records. No matching VM service/process found. Hyper-V CIM namespace `root/virtualization/v2` returns `Invalid namespace`. Standard user VirtualBox/VMware and public Hyper-V VM directories are absent. |
| Other existing local Linux environment | No Ubuntu, Debian, SUSE, Kali or Linux distribution Appx package found; no WSL service/process found. No `bash`, `podman`, `limactl` or `colima` command found. These are local inventory checks, not a claim that every possible custom disk location was searched. |
| Docker | `docker` command absent; standard Docker installation directory absent; no matching Docker service/process or uninstall record found. Docker version cannot be reported. CLI missing is the observed blocker; daemon availability and Linux-user daemon permissions cannot be tested. |

Initial sandbox restrictions prevented CIM/Appx inventory. Those read-only queries were repeated outside the sandbox successfully; Hyper-V's missing namespace is the resulting host observation, not a sandbox-denial inference.

**Exact prerequisite to resume:** an operator-provisioned, bootable Linux environment with an unprivileged user, preferably WSL2 Ubuntu on this host, or an explicitly supplied existing Linux VM. It needs a Linux-side checkout containing the current implementation (including currently uncommitted files), Python 3.12 with venv, native build prerequisites and access to the existing GGUFs. Use Linux filesystem storage for the checkout and disposable application state; a read-only shared model location is suitable. Do not assume a clone of the remote repository contains this working tree's uncommitted deployment implementation. Docker acceptance additionally requires Docker Engine running in that Linux environment, the Compose plugin and usable daemon access for the acceptance user. Installing these prerequisites requires explicit authorization; none was inferred from this task.

## 21. Follow-up acceptance matrix

Historical Windows verification in sections 3–17 remains separate from future target-platform acceptance. **No Linux/Docker application command, build, model inference or automated suite executed in this follow-up.** DEFERRED means intentionally not yet tested by project decision, not an application failure or an immediate development blocker. All acceptance rows are retained; no Linux/Docker item has been promoted to VERIFIED.

| Acceptance item | Windows evidence already recorded | Native Linux | Docker |
| --- | --- | --- | --- |
| Clean checkout, Python, venv, dependencies, CLI installation | Existing Windows development environment only | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Relative paths, executable bits, case sensitivity | Does not establish Linux behavior | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| CLI help/doctor/status/models list/models verify/validate | Selected commands executed; see section 15 | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Native CPU generation, streaming, embeddings, model reuse | VERIFIED for recorded Windows scope | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Ingestion/search/RAG/citations/logs/incidents/frontend | VERIFIED for recorded Windows scope | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Clean stop/restart, persisted documents/inventory/incidents/retrieval/citations | VERIFIED for application lifecycle scope | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Forced-stop recovery with disposable data | VERIFIED for recorded Windows process scope | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Backup manifest/checksums/metadata/identity and clean restore | VERIFIED for recorded Windows scope | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Unprivileged user; configuration/data/uploads/models/vector/backups/log permissions | Linux modes not exercised | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Optional systemd user/directory/environment/restart/graceful stop | NOT APPLICABLE | DEFERRED / NOT YET TESTED; example reviewed only | NOT APPLICABLE; Compose manages lifecycle |
| Docker version and daemon access classification | CLI missing on host | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Compose config/build/up and all intended services | Static review only | NOT APPLICABLE to native runtime | DEFERRED / NOT YET TESTED |
| Container frontend/backend/networking/health/readiness | NOT APPLICABLE | NOT APPLICABLE to native runtime | DEFERRED / NOT YET TESTED |
| Read-only model mounts, model identity, weights excluded from image/context | Configuration review only | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Persistence across stop/start and container recreation | NOT APPLICABLE | NOT APPLICABLE to native runtime | DEFERRED / NOT YET TESTED |
| Backup/restore against actual container volumes | NOT APPLICABLE | NOT APPLICABLE to native runtime | DEFERRED / NOT YET TESTED |
| OS-isolated offline startup/inference/RAG/logs/incidents | VERIFIED only at Windows Python-process scope | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Resource limits, insufficient-memory recovery and observed target memory | Windows snapshots in section 6 only | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Clean exit/container restart/backend failure/Compose restart policy | NOT APPLICABLE | DEFERRED / NOT YET TESTED for Linux signal behavior | DEFERRED / NOT YET TESTED |
| Actual published ports, internal services and health-check addresses | Configuration review only | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Legacy index rejection, explanation and re-ingestion path | Compatibility checks/documentation in section 11 | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| Frozen development and held-out reliability regression | Prior VERIFIED results in section 17 | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |
| All existing automated tests on target platform | Prior VERIFIED: 389 backend + 5 frontend | DEFERRED / NOT YET TESTED | DEFERRED / NOT YET TESTED |

Prior reliability evidence records development supported/oracle **10/10**, refusals **4/4**, and held-out supported/oracle **22/22**, refusals **5/5**, with passing logs/incidents. The saved aggregate summary does not separately enumerate a **2/2 adversarial** result; that requested gate must be explicitly recorded on the target run. None of these historical results is a fresh Linux/Docker regression run. No tests were rerun for this report-only follow-up.

**Current sign-off: Windows-native milestone CLOSED, VERIFIED within recorded scope; Linux and Docker DEFERRED / NOT YET TESTED.** A supervised first read-only homelab integration may proceed on Windows as described in section 19. Execute the retained native Linux and Docker acceptance procedures when those deployment tasks are explicitly resumed.

## 22. Runtime recheck after provisioning notice — 29 October 2026, 18:51 UTC

Historical attempt, superseded by section 23. The saved command output is unchanged; no further runtime search or provisioning is requested. References below to the unchanged BLOCKED matrix describe its state at the time of that attempt.

**Still BLOCKED on this host.** The requested prerequisite checks were repeated both inside and outside the sandbox. The outside-sandbox execution confirms the following; complete command results, exit codes and the observed Windows 11 build 26200 platform are saved in [runtime recheck evidence](deployment-evidence/runtime-recheck-2026-10-29.json).

| Required prerequisite | Executed check | Observed result |
| --- | --- | --- |
| WSL/Linux runtime | `wsl --status`; `wsl --list --verbose` | Exit 50 and exit 1 respectively; both report that WSL is not installed. The launcher exists at `C:\WINDOWS\system32\wsl.EXE`. |
| Linux distribution/version | `wsl --exec cat /etc/os-release` | Exit 1; WSL is not installed, so no distribution/version could be read. |
| Docker CLI | `docker --version` | Executable not found (`WinError 2`); command lookup also returns no Docker executable. Standard Docker CLI/Desktop paths are absent. |
| Docker daemon | `docker info` | Cannot execute because the CLI is absent. Daemon availability is unverified, not independently established as absent. |
| Docker Compose | `docker compose version` | Cannot execute because the CLI is absent. Compose version is unverified. |

The earlier section 20 inventory remains historical evidence; this recheck does not claim to repeat every VM discovery query. No alternate VM/SSH target or custom runtime path was supplied for this attempt. A request for that missing location was raised. No operating-system installation or restart was performed.

The section 21 acceptance matrix is preserved without any BLOCKED-to-VERIFIED changes. Clean Linux setup, GGUF generation/streaming/embeddings, CLI/browser workflows, persistence/forced-stop recovery, backup/restore, permissions, Docker builds/Compose/health/readiness/mounts/networking, offline operation, legacy-index compatibility and development/held-out regression runs remain unexecuted on the target platforms in this attempt. Historical Windows results do not satisfy those gates. No application code, features, deployment configuration, models or evaluation fixtures were changed, and no application suites were rerun on Windows as a substitute.

**Sign-off after this recheck:** native Linux deployment **BLOCKED, not VERIFIED**; Docker deployment **BLOCKED, not VERIFIED**; Grimoire **not ready for acceptance sign-off for its first real read-only homelab integration**. Resume with access to the provisioned Linux runtime (an accessible WSL distribution, an explicitly supplied VM/SSH target, or the custom runtime location), then execute the existing procedure and record each successful target-platform result before changing its matrix status.

## 23. Intentional deployment deferral and current-environment closure — 29 October 2026

The project owner confirmed that WSL and Docker are intentionally absent from this Windows machine and are not wanted at this stage. Do not continue searching for or provisioning Linux, WSL, Docker, VMs, Hyper-V, SSH targets or other runtimes. No installation is authorized or needed for this milestone.

| Scope | Current decision |
| --- | --- |
| Windows native development/runtime | VERIFIED to the extent already documented in sections 3–17 and the saved evidence; milestone CLOSED for this environment |
| Linux native deployment | DEFERRED / NOT YET TESTED; future release/deployment task |
| Docker deployment | DEFERRED / NOT YET TESTED; future release/deployment task |
| First real read-only homelab integration | Ready to proceed as a supervised Windows-native trial within section 19's scope; integration itself has not yet executed |

The section 21 matrix retains every acceptance item and changes unexecuted target-platform statuses to DEFERRED / NOT YET TESTED, never VERIFIED. Sections 20 and 22 and their machine-readable evidence remain historical records. Their earlier blocker language and prerequisite requests are superseded by this decision. Linux/Docker support, Compose/Dockerfiles, systemd examples and acceptance procedures are preserved without weakening. Further development does not depend on installing these runtimes. Future platform verification must still execute the corresponding checks successfully.

This closure is a scope decision, not a new test result. Existing Windows evidence includes real-model restart and restore, abrupt TCP-server recovery with identical citations, 389 backend and 5 frontend tests, and passing development/held-out reliability suites. No tests were rerun for this documentation-only decision; no product code, runtime configuration, model, evaluation fixture or historical evidence file was changed. Existing resource, signal handling, offline-isolation and model-quality limitations still apply.
