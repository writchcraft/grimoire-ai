# Linux deployment and recovery

Linux is the primary deployment target. The documented target is Ubuntu Server 24.04 LTS, Python 3.12, CPU inference, one backend worker, and pre-provisioned Qwen2.5-1.5B Q4_K_M plus MiniLM F16 weights. Docker Compose is the primary deployment path; native installation is useful for acceptance and troubleshooting.

**Current-environment milestone: CLOSED. Windows native development/runtime: VERIFIED to the extent documented. Linux native deployment and Docker deployment: DEFERRED / NOT YET TESTED by project decision.** The Windows development host intentionally has neither WSL nor Docker installed. Do not search for or provision alternative runtimes to resolve this deferral. Linux and Docker remain supported deployment targets; their existing configuration and acceptance procedures below are retained for a future release/deployment task. Their untested status does not block continued Windows-native development or a supervised first read-only homelab integration. Configuration review does not establish runtime verification. See [the deployment report](deployment-report.md) for evidence, scope and limitations.

## Compose startup

```bash
git clone https://github.com/writchcraft/grimoire-ai.git
cd grimoire-ai
cp .env.example .env
mkdir -p models
# Explicitly provision models/chat.gguf and models/embed.gguf.
# Use docs/acceptance-models.json for the existing models' revisions and SHA-256.
# Set GRIMOIRE_EMBEDDING_MODEL=all-MiniLM-L6-v2-f16 in .env for these weights.
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:7864/health
```

The initial `up` builds missing images; use `docker compose build` explicitly to capture build acceptance. Grimoire never downloads weights. The model directory must already exist and be readable by UID/GID 10001; bind mounts refuse to create a missing host directory. Model paths in `.env` refer to the container. Package/image acquisition and model provisioning happen before offline runtime.

Only the frontend's loopback port 7864 is published. Nginx proxies `/api/`, `/health`, and `/ready` to the backend. The backend has no host port. Do not expose the unauthenticated API to an untrusted network. For another hostname, explicitly configure `GRIMOIRE_ALLOWED_HOSTS` as a JSON array in `.env` and supply authenticated ingress separately.

```bash
docker compose logs --tail=100 backend
docker compose stop       # graceful; up to two minutes before forced termination
docker compose start
docker compose restart backend
docker compose down       # retains named volumes
# Do not use down -v unless intentionally deleting application data.
```

The backend runs as UID 10001 with a read-only root filesystem, dropped capabilities, no privilege escalation, an explicit data volume and a bounded temporary filesystem. Models are read-only. Frontend and backend logs rotate at 10 MB × 3 files per container. No resource cap is imposed by default. The optional Ollama profile is unchanged and is outside this native CPU acceptance.

## Native Linux setup and acceptance

The following is the Linux procedure to execute on a disposable host; it has **not** run on this Windows machine. Install Python 3.12/venv, a C/C++ compiler, CMake, and the OpenMP runtime using your host's package manager first. Then, as an unprivileged user:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements-native.lock
python -m pip install --no-deps -e './backend[native]'
export GRIMOIRE_AI_PROVIDER=native
export GRIMOIRE_EMBEDDING_PROVIDER=native
export GRIMOIRE_NATIVE_MODEL_PATH="$PWD/models/chat.gguf"
export GRIMOIRE_NATIVE_EMBEDDING_MODEL_PATH="$PWD/models/embed.gguf"
export GRIMOIRE_EMBEDDING_MODEL=all-MiniLM-L6-v2-f16
export GRIMOIRE_DATA_DIR="$HOME/.local/share/grimoire"
export GRIMOIRE_NATIVE_CONTEXT_SIZE=4096
export GRIMOIRE_NATIVE_EMBEDDING_CONTEXT_SIZE=512
export GRIMOIRE_NATIVE_THREADS=2
export GRIMOIRE_NATIVE_GPU_LAYERS=0
export GRIMOIRE_MAX_OUTPUT_TOKENS=384
export GRIMOIRE_CONTEXT_CHARS=8000
grimoire doctor
grimoire status
grimoire models list
grimoire models verify
grimoire validate --json
grimoire serve
```

`serve` serves frontend and backend on the same loopback origin, from an editable repository installation. It reads exported environment variables, not Compose's `.env`. Confirm the UI can upload, search, chat with citations, analyse logs, generate an incident and save inventory. Use Ctrl+C or SIGTERM, wait for `application_stopped`, start again and repeat search and the same RAG question. Never run multiple writers against one data directory; keep one server/worker. CLI ingestion also opens its own index: stop the server before CLI writes, then restart to rebuild vectors. Online backup uses SQLite's snapshot API and does not open a competing index.

The opt-in rehearsal below requires the dev test dependencies (`pip install -e './backend[dev]'`). It loads real local models, uses temporary state, repeats application lifecycles, restores a backup, asserts identical citation records, checks frontend assets and denies non-loopback Python sockets. It records platform, memory snapshots, process CPU time, disk size and closed handles:

```bash
python scripts/deployment_acceptance.py --output .cache/deployment/results.json
grimoire evaluate --suite all --json --output .cache/deployment/reliability.json
```

The rehearsal uses the real ASGI app via TestClient, not a real TCP server or systemd. Separately test `serve` and frontend traffic on the host. Abrupt-stop tests use disposable subprocess databases:

`python scripts/service_acceptance.py` separately starts the actual `grimoire serve` process, uses real loopback HTTP, ingests data, saves inventory/incidents, kills the process, restarts and checks the same cited answer. It also runs `doctor`, `status`, `models list` and `validate`. On Windows both process stops are abrupt; the clean lifecycle rehearsal above supplies graceful resource-close coverage. On Linux the second process stop uses SIGTERM. This script does not block outbound networking; use the separate offline rehearsal and isolated host-level test for that claim.

```bash
python -m pytest backend/tests/test_deployment.py -q
```

On Linux, also kill a disposable **server** with SIGKILL during ingestion, restart and verify committed documents, absence of a partial document, inventory, vector retrieval and cited RAG. Tests of `os._exit` at transaction boundaries do not establish OS power-loss or filesystem durability. Do not perform this against real user state.

## Filesystem and persistence contract

| Component | Compose | Native Linux recommendation | Backup |
| --- | --- | --- | --- |
| Application code and frontend | image `/app`, Nginx static assets | `/opt/grimoire-ai`, editable checkout | reproducible Git revision |
| Configuration | host `.env`, explicitly forwarded settings | `/etc/grimoire/grimoire.env`, owner-readable | separately, securely; excluded from data backup |
| Model weights / optional registry | host models → `/models:ro` | `/srv/grimoire/models`, read-only to service | revision + full checksum; weights reproducibly obtainable |
| Relational persistence | `grimoire-data:/app/data` | `GRIMOIRE_DATA_DIR=/var/lib/grimoire` | SQLite snapshot |
| Uploaded originals | `originals` table in SQLite | same | included for newly ingested documents |
| Document metadata, chunk text, vectors, FTS | SQLite | same | included |
| Vector index | in-memory Qdrant | same | rebuilt from persisted vectors; no separate index directory |
| Service inventory and incidents | SQLite `services`, `incidents` | same | included |
| Application logs | stderr, Docker log driver | stderr/journald | optional audit retention, not recovery state |
| Caches and temporary files | `/tmp` tmpfs; no model download cache | OS temporary directory | excluded |
| Backups | explicit operator-mounted `/backups` | separate protected disk/directory | copy off host according to operator policy |

The local development default remains `data/runtime`; set an absolute `GRIMOIRE_DATA_DIR` for a service. There are no per-upload filesystem paths: filenames and source paths are labels, validated and stored in SQL, never used to write outside storage. Original bytes commit in the same transaction as document metadata/chunks/vectors. Existing documents from earlier versions have no original-byte archive; retain the original input files for re-ingestion. Chat history remains browser/session state, not a server transcript. Log analysis is transient; generated incident Markdown and its analysis are now persisted and returned with an ID/timestamp. `GET /api/incidents?limit=100` lists records (maximum 1000); `DELETE /api/incidents/{id}` supports deliberate retention pruning.

SQLite WAL and SHM files belong to the active database. Do not delete them or copy just a live database file. There are no loose upload or incident temporary files to reconcile after a crash. Completed SQL transactions are authoritative; the vector index reconstructs on restart. A stale in-memory vector cannot return text absent from SQL. One server owns the in-memory index; independent writers are not supported.

## Health and shutdown

* `/health`: HTTP 200 for application liveness, no database scan or model inference. Compose uses this to start the UI even when weights are unavailable or generation is lazy.
* `/ready`: HTTP 200 only when generation and embeddings report `ready` and SQLite is readable; HTTP 503 otherwise. It makes no inference calls and performs only a small SQL probe. Storage readiness is readability, not a guarantee of disk capacity or writability under every future load.
* `/api/status`: richer diagnostic state, including document counts; avoid using this as a frequent liveness probe.

Generation is lazy in the existing registry lifecycle. `/ready` intentionally remains 503 until the first successful generation load/use; it must not gate the UI that initiates that load. A ready model may still reject oversized input. Startup fails clearly for incompatible storage. Native load/generation failures log safe events and return actionable provider errors. JSON logs contain levels, request IDs, route templates, status and duration; exclude query strings, bodies, uploaded text, native exception details and configuration secrets. Use `GRIMOIRE_LOG_LEVEL` to control verbosity. Standard server startup/shutdown messages can also appear on stderr.

Shutdown drains active native calls, closes generation/embedding workers, SQLite connections and Qdrant handles. This can take longer than an HTTP timeout; increase the two-minute stop grace period for slow hardware before accepting real traffic. SIGKILL bypasses shutdown. After deletion, SQLite may retain free pages: physical file size need not shrink. If needed, run SQLite `VACUUM` only offline with sufficient free disk and a verified backup.

## Backup and restore

```bash
# Can snapshot an active server: the SQLite backup API includes committed WAL data.
grimoire backup create /srv/grimoire-backups --json
# Result names a unique grimoire-<id> directory with manifest.json + grimoire.sqlite3.
# Stop the service before switching its configured data directory.
grimoire backup restore /srv/grimoire-backups/grimoire-<id> /var/lib/grimoire-restored --json
export GRIMOIRE_DATA_DIR=/var/lib/grimoire-restored
grimoire doctor
grimoire serve
```

The restore destination must **not exist**. Set ownership to the service account before startup; run backup and restore as that account into pre-authorized parent directories. Point the service configuration at the new directory, restart, inspect documents, inventory, incidents and search, then ask the same cited RAG question. Keep the previous directory until this succeeds. Rehearsal with actual models has succeeded on Windows; Linux/container restore remains unexecuted.

Snapshots include documents, original upload bytes available at backup time, chunk text/vectors, metadata, FTS, services and incidents. A versioned manifest contains application version, UTC timestamp, schema version, component counts, embedding identity, vector dimension/backend and database SHA-256. Qdrant is reconstructed, not serialized. Manifest and database are validated before restore publication. Both operations use temporary directories and atomic rename; a failed operation cleans its staging directory. After process/host failure, unpublished `.grimoire-backup-*` / `.grimoire-restore-*` directories are incomplete staging data and must not be restored; remove them only after confirming no backup process is active. Files/directories are flushed before publication on supported OS APIs; this is not a power-loss guarantee.

Backup paths reject symlink traversal, backup creation rejects locations inside active state, and restore never overwrites a directory. Use trusted operator-owned parent directories; this is not a defense against a malicious local administrator racing filesystem changes. Checksums detect accidental modification, not malicious replacement of both manifest and database. Backups contain user data, potentially including secrets in uploaded content; protect them accordingly. Configuration/secrets are not copied automatically. Model weights, logs and caches are excluded. No automatic retention or scheduler is installed: retain a small number of generations and an off-host copy under your own policy.

For Compose, explicitly mount a protected host backup directory. Create it readable/writable by container UID 10001 before running the command:

```bash
docker compose run --rm --no-deps -v "$PWD/backups:/backups" backend \
  grimoire backup create /backups --json
docker compose stop backend
docker compose run --rm --no-deps -v "$PWD/backups:/backups:ro" backend \
  grimoire backup restore /backups/grimoire-<id> /app/data/recovered --json
# Set GRIMOIRE_DATA_DIR=/app/data/recovered in .env, then recreate backend.
docker compose up -d
```

This keeps the old database in the data volume and switches to a new subdirectory. Use a fresh destination for each restore. **These Compose commands are documented, not executed here.** The backup workflow needs no inference or network; models need not load for it.

## Compatibility

Storage schema uses SQLite `user_version=1`. The existing unversioned schema (0) receives additive originals/incident tables and missing heading/lineage columns; user documents remain intact. Unknown versions fail with a schema error rather than being silently opened. Application patch version alone is not an incompatibility: schema and embedding identity are authoritative. Backup format version 1 and schema must be supported at restore time.

Native embedding identity now uses the configured embedding identifier and a **full SHA-256 of weights**, independent of path/mtime/inode. Identical weights can move between machines. Replacing any byte or changing identity refuses a populated index; nonfinite, malformed or mixed-dimension persisted vectors fail at startup. Query/ingest dimension changes are rejected. **Legacy native fingerprint identities require re-ingestion into a new data directory**; do not edit metadata to bypass the check. Before upgrading, retain source files and back up old state with the old version's stopped-filesystem procedure. The new backup command requires an initialized schema-1 database. Existing Ollama model-name identity remains unchanged; pin its model revision externally. Full schema migrations and automatic re-embedding are not implemented.

## Resources, disk and offline operation

The [measured report](deployment-report.md) gives Windows observations, not Linux capacity claims. Model size dominates disk, while model/context buffers dominate resident memory. Persistent vectors are JSON in SQLite, and Qdrant duplicates them in memory. Large-corpus rebuild time and memory remain unmeasured. Upload byte/chunk limits bound each request, not total corpus growth. Prune obsolete documents/incidents, rotate logs, monitor free disk and manage backup retention. Native runtime creates no model cache; build/pip caches belong to provisioning, outside application recovery state.

An optional experiment is provided, with **no hard limits in the default deployment**:

```bash
docker compose -f docker-compose.yml -f deploy/compose.resources.example.yml up -d
docker stats --no-stream
docker compose logs --tail=100 backend
```

The example sets two backend CPUs/threads and 4 GiB memory, plus a small frontend limit. It is unverified and may require adjustment for other weights, context or corpus. `GRIMOIRE_NATIVE_THREADS` sets both generation and batch thread counts, but is not an OS CPU quota. Linux memory-limit/OOM behaviour must be tested on the target host. For native installs, apply optional systemd CPUQuota/MemoryMax in an operator drop-in and verify with your actual corpus; do not assume a Python thread setting caps process memory.

After acquiring images, dependencies and checksummed weights, perform acceptance with outbound access denied in an isolated Linux VM/network namespace. Verify startup, assets, generation, embeddings, ingestion, search, cited RAG, logs, incidents, inventory, restart and restore. The native pipeline has passed with non-loopback Python sockets denied; that does not prove OS-level offline container behaviour. No telemetry, cloud fallback, SSH execution or extra integration has been introduced.

## Optional systemd example

[deploy/grimoire.service](../deploy/grimoire.service) runs the editable native install as an existing unprivileged `grimoire` user. Provision `/opt/grimoire-ai`, `/etc/grimoire/grimoire.env`, `/srv/grimoire/models` and writable `/var/lib/grimoire` explicitly; root is needed only for host installation/ownership setup, not application runtime. Put absolute `GRIMOIRE_` paths in the environment file, set data to `/var/lib/grimoire`, and keep configuration protected. The unit restricts writes to that data directory, uses journald, restarts on failure, and allows graceful SIGTERM shutdown. Journald retention is host-managed. It is a reviewed example, **not systemd acceptance**. Compose remains the primary route.
