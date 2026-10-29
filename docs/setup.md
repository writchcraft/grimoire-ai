# Setup and operations

## Native GGUF inference (default)

Ollama is not required. A Hugging Face Safetensors directory cannot be used as `NATIVE_MODEL_PATH`: this setting must name a compatible GGUF file. Conversion/quantization is a separate provisioning step; renaming a file is not conversion. Generation and embeddings load separate local GGUF files through llama-cpp-python. Grimoire never downloads weights during startup. Obtain compatible files explicitly, check their licenses and provenance, and retain them outside Git. A Qwen2.5 3B Instruct GGUF is an example chat model; use a pooled embedding GGUF for retrieval, not the chat model. Qwen2.5 1.5B Q4_K_M and MiniLM F16 have now executed on this host; see [real-model acceptance](real-model-acceptance.md) for exact provenance, CPU configuration and quality limits.

For Docker, copy `.env.example` to `.env`, create `models/`, and place generation weights at `models/chat.gguf` and embedding weights at `models/embed.gguf`. Set `GRIMOIRE_EMBEDDING_MODEL` to an identifier for your actual embedding model. Configure a different host directory with `GRIMOIRE_MODELS_DIR`; the backend sees it read-only at `/models`. Model paths in `.env` are container paths. Then run:

```bash
docker compose up -d --build
```

Open http://127.0.0.1:7864/. The frontend publishes only on loopback port 7864 (`GRIMOIRE_PORT` overrides the host port; Nginx still listens on 8080 inside its container). The backend persists SQLite/embeddings in `grimoire-data`; model files persist in your host directory, not the image. Keep the directory readable by container UID/GID 10001. The CPU image includes build tools because llama-cpp-python may compile from source; builds may take time and need RAM. No CUDA installation is assumed. The optional Ollama service is not started by default.

For Python development, install Python 3.12+ and a native C/C++ toolchain if no suitable wheel is available. Linux generally needs a compiler and CMake; Windows source builds need a supported Visual Studio C++ toolchain or another toolchain supported by the binding. See the [binding installation instructions](https://llama-cpp-python.readthedocs.io/en/latest/). From repository root:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend/requirements-native.lock -e './backend[native,dev]'
$env:GRIMOIRE_AI_PROVIDER = 'native'
$env:GRIMOIRE_EMBEDDING_PROVIDER = 'native'
$env:GRIMOIRE_NATIVE_MODEL_PATH = './models/chat.gguf'
$env:GRIMOIRE_NATIVE_EMBEDDING_MODEL_PATH = './models/embed.gguf'
$env:GRIMOIRE_EMBEDDING_MODEL = 'my-pinned-embedding-model'
.venv\Scripts\python.exe scripts/dev.py
```

On Linux/macOS use your environment's Python/pip and `export GRIMOIRE_NATIVE_MODEL_PATH=/path/to/chat.gguf` (and the other variables above). The development script serves UI/API on loopback 7864 (`GRIMOIRE_PORT` overrides it) and reads shell variables; it does not load Compose's `.env`. API-only: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log`.

For tests or Ollama-only development, `pip install -r backend/requirements.lock -e './backend[dev]'` suffices; importing the application does not require the native binding. Starting without paths/bindings still allows status, service inventory, existing lexical search and deterministic log/incident analysis.

## Historical small CPU candidate setup (superseded)

The actual provisioned setup uses Qwen2.5 **1.5B** Q4_K_M with MiniLM F16; follow [the current acceptance configuration](real-model-acceptance.md#local-configuration). On Windows, `pip install -r backend/requirements-native-windows.txt` installs the locked maintainer CPU wheel. The earlier 0.5B proposal below was not downloaded.

The 29 October real-model acceptance attempt stopped before inference: no usable local GGUF weights or installed `llama_cpp` binding were found. These are specific **provisioning candidates**, not models already tested with Grimoire. A small model can validate execution without establishing production answer quality.

Download these two files explicitly using the file download controls in the linked repositories; do not clone/download every quantization:

| Role | Repository and exact file | Local destination |
| --- | --- | --- |
| Generation | [Qwen/Qwen2.5-0.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/tree/main), `qwen2.5-0.5b-instruct-q5_k_m.gguf` | `models/chat.gguf` |
| Embeddings | [second-state/All-MiniLM-L6-v2-Embedding-GGUF](https://huggingface.co/second-state/All-MiniLM-L6-v2-Embedding-GGUF/tree/main), `all-MiniLM-L6-v2-ggml-model-f16.gguf` | `models/embed.gguf` |

The upstream listings advertise approximately 522 MB and 46 MB respectively; actual local sizes must be recorded after acquisition. Retain each repository revision, license and file SHA-256. The embedding GGUF is a conversion supplied by Second State. The [original MiniLM model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) specifies mean pooling and **384 dimensions**; verify those dimensions with the actual GGUF rather than relying on the conversion card's inconsistent context/vector labels. Use short example documents initially. The original encoder targets short paragraphs (its sentence-transformers default truncates beyond 256 word pieces); Grimoire disables truncation, and a configured context does not extend a model's trained capacity. Longer inputs and pooled GGUF compatibility still need acceptance.

Install the native dependencies using the development command above. This is a separate, explicit dependency acquisition step; on Windows it may require CMake and Visual Studio C++ build tools. No dependency installation or weight acquisition was executed in this acceptance attempt. Then, from repository root in PowerShell:

```powershell
$env:GRIMOIRE_AI_PROVIDER = 'native'
$env:GRIMOIRE_EMBEDDING_PROVIDER = 'native'
$env:GRIMOIRE_NATIVE_MODEL_PATH = (Resolve-Path './models/chat.gguf').Path
$env:GRIMOIRE_NATIVE_EMBEDDING_MODEL_PATH = (Resolve-Path './models/embed.gguf').Path
$env:GRIMOIRE_EMBEDDING_MODEL = 'all-MiniLM-L6-v2-f16-acceptance-v1'
$env:GRIMOIRE_NATIVE_CONTEXT_SIZE = '4096'
$env:GRIMOIRE_NATIVE_EMBEDDING_CONTEXT_SIZE = '512'
$env:GRIMOIRE_NATIVE_GPU_LAYERS = '0'
$env:GRIMOIRE_MAX_OUTPUT_TOKENS = '512'
# Separate acceptance storage; do not replace an existing knowledge database.
$env:GRIMOIRE_DATA_DIR = './.cache/real-model-acceptance/runtime'
Get-FileHash './models/chat.gguf', './models/embed.gguf' -Algorithm SHA256
.venv\Scripts\grimoire.exe status
.venv\Scripts\grimoire.exe doctor
.venv\Scripts\grimoire.exe models list
.venv\Scripts\grimoire.exe models inspect
.venv\Scripts\grimoire.exe models verify
.venv\Scripts\grimoire.exe validate --json
.venv\Scripts\grimoire.exe serve
```

If the console script is unavailable, use `.venv\Scripts\python.exe -m app.cli` with the same arguments. Leave thread count and chat-format overrides unset initially so the binding uses its defaults/model template; record the resolved configuration during acceptance. GPU testing is optional and requires explicit configuration plus a compatible binding build, as described below.

`models verify` loads and performs minimal inference, then closes its runtimes. Subsequent CLI commands are separate processes and do not share loaded models. Read-only `doctor`/`status`/`models inspect` deliberately remain **unverified** for native inference even after a successful verification command; they do not persist a readiness certificate. Check `/api/status` in the running application for loaded readiness, and run two requests plus streaming in that same process to establish reuse. `validate` still uses synthetic vectors and cannot certify the real pipeline.

Continue with the [demo evidence walkthrough](demo.md) and record results in the [existing validation record](validation.md). Include a multi-document question such as “Which ports do Grafana and PostgreSQL use?” (3000 and 5432), with the proxy document as a distractor. The unsupported Grafana-password question must refuse even if related excerpts are retrieved. For ambiguous logs, repeated connection refusals alone cannot establish whether the backend stopped, the endpoint is wrong or a network rule rejects traffic. Inspect every claim and citation manually.

Use the isolated index to check duplicate ingestion, changed-content versions, restart reconstruction, deletion and an intentional embedding-identity mismatch. Keep original input files for re-ingestion; follow the reindex procedure below. Do not replace weights in place under an unchanged identity. Repeat the real workflow with outbound access denied only in an isolated environment after dependencies and weights are present. Record unexpected network attempts and resource observations; neither offline real inference nor timings have been measured yet.

## Command-line interface

The backend package installs `grimoire`. Activate the environment after the editable install above (PowerShell: `.venv\Scripts\Activate.ps1`, POSIX: `source .venv/bin/activate`). For an existing installation, repeat `python -m pip install --no-deps -e ./backend` to register the command. `python -m app.cli` is also supported.

```bash
grimoire doctor
grimoire serve
grimoire status
grimoire knowledge ingest ./data/examples/
grimoire knowledge search "grafana port"
grimoire models inspect
grimoire models verify --role generation
grimoire logs analyse ./data/examples/proxy-incident.log
grimoire demo
grimoire validate --json
```

Run examples from the repository root. Commands read the same shell `GRIMOIRE_` settings as the API; they do not load Compose's `.env`. `serve` shares the development web entry point, binds loopback, and honors `GRIMOIRE_PORT` (7864 by default). Frontend assets require an editable repository installation; an isolated backend wheel still supports other commands.

`status` reports a local configuration snapshot, not an already running server's state. Native readiness remains unverified until loaded; use web status for the running process. Model listing/inspection reads file metadata and the GGUF header without inference. A valid header does not prove compatibility. `models verify` explicitly loads selected models and runs minimal generation (16-token cap) or embeddings. Loading can take time and memory; weights are never downloaded.

`doctor` is read-only: settings/runtime, model files/readability, binding discovery, directory permissions, SQLite integrity/embedding identity, web assets/hosts/port, endpoint locality and optional Docker/Ollama checks. Unconfigured optional Ollama is skipped. Missing Docker CLI produces **SKIP**; daemon detection is bounded to five seconds. A configured unavailable provider/model fails. Binding discovery does not prove native libraries load; use `models verify`. Permission checks are advisory OS access checks, not proof that every later write succeeds.

`validate` distinguishes **VERIFIED**, **UNVERIFIED**, **BLOCKED**, and **NOT APPLICABLE**. It imports the application and exercises production ingestion/retrieval/storage with explicitly synthetic embeddings in temporary storage with networking denied, including persistence/vector rebuild and no-evidence behavior. It does not alter the configured index or run inference. Missing Docker leaves Docker validation **UNVERIFIED**. Configured index rebuild, real-model relevance, disconnected deployment and container builds remain unverified unless separately exercised.

Ingestion writes to the configured index. Directory input is non-recursive, handles at most 512 supported UTF-8 files, skips unsupported extensions, rejects symlink files and enforces application byte/chunk limits. Input syntax is checked before opening storage; later failures may leave earlier files ingested. Re-running identical input uses existing deduplication. Search uses the same lexical retrieval as web Search and prints source paths/lines. Log analysis requires a real model explanation and fails clearly without generation. `demo` prints a guided sequence using public examples and the existing demo guide; it makes no model or integration claims.

Diagnostic and data commands accept `--json`. Exit codes: **0** means the command/report completed without blocking failures (warnings/skips may remain); **1** means an operational failure or FAIL/BLOCKED check; **2** means invalid arguments/settings; **130** means interrupted. Status and inspection return 0 when their report succeeds, including when readiness is explicitly degraded/unverified. JSON is on stdout; runtime logs may appear on stderr.

## Configuration and lifecycle

All settings use `GRIMOIRE_`. `AI_PROVIDER` and `EMBEDDING_PROVIDER` independently select `native` or `ollama`; invalid values fail validation. See the README configuration table and `.env.example` for defaults.

Native settings: `NATIVE_MODEL_PATH`, `NATIVE_EMBEDDING_MODEL_PATH`, `NATIVE_CONTEXT_SIZE` (8192), `NATIVE_EMBEDDING_CONTEXT_SIZE` (2048), `NATIVE_GPU_LAYERS` (0), optional positive `NATIVE_THREADS`, optional `NATIVE_CHAT_FORMAT`, and `MAX_OUTPUT_TOKENS` (2048). Blank optional settings are allowed. The native context setting is also used for Ollama's generation context. GGUF chat templates are selected by the binding; set a format override only when required by your model.

Loading starts once per application startup in background workers. `/api/status` reports `ai` and `embeddings` separately with provider, backend, model basename/name, context and native GPU-layer configuration. States are `not_configured`, `model_missing`, `loading`, `ready` and `failed`; failures provide a safe reason. `/health` remains process liveness. Status never performs a generation or downloads weights. Restart after fixing files/settings; no reload/retry control exists yet. A loaded model can still reject an oversized prompt; readiness is not a quality or prompt-capacity guarantee.

General chat and model-assisted log/incident explanation need generation only. Document ingestion and knowledge retrieval need embeddings too. Select a pooled sentence embedding model and honor its model-specific input conventions (including prefixes if needed). Grimoire passes text as-is and has no automatic prefix adapter. Embedding truncation is disabled; excessively long chunks/queries return an actionable failure instead of silently changing vectors. Test retrieval against your own corpus. Native embeddings and generation are independent instances and consume memory separately.

Use one backend worker. Native generation serializes all model-assisted requests and streaming holds its runtime lock. Embeddings can run independently. Work runs off FastAPI's event loop. Cancellation waits for the active native call before releasing the model; non-streaming generation and model loading cannot be forcibly interrupted. `REQUEST_TIMEOUT` controls Ollama HTTP calls only. There is no throughput or response-time guarantee.

## CPU and GPU

CPU is the default (`NATIVE_GPU_LAYERS=0`). RAM and latency depend on quantization, model size, context, threads and hardware. Reduce context/output limits if necessary; character budgets are approximate and do not ensure a prompt fits.

GPU offload requires a matching llama-cpp-python build and hardware/runtime. Merely setting `NATIVE_GPU_LAYERS=-1` does not install GPU support. CUDA builds may use `CMAKE_ARGS=-DGGML_CUDA=on` when explicitly installing the binding, as described in its documentation. macOS Metal and other backends depend on supported binding builds. The supplied Linux CPU Docker image has no GPU device passthrough or GPU build; use an explicitly customized image/deployment for acceleration. GPU and platform-specific native execution remain unvalidated here.

## Optional Ollama compatibility

To use Ollama for both subsystems, set:

```dotenv
GRIMOIRE_AI_PROVIDER=ollama
GRIMOIRE_EMBEDDING_PROVIDER=ollama
GRIMOIRE_MODEL=qwen2.5:3b
GRIMOIRE_EMBEDDING_MODEL=nomic-embed-text
GRIMOIRE_OLLAMA_URL=http://ollama:11434
```

Explicitly enable the profile and fetch models yourself:

```bash
docker compose --profile ollama up -d --build
docker compose exec ollama ollama pull qwen2.5:3b
docker compose exec ollama ollama pull nomic-embed-text
```

For a host-managed Ollama server use `http://127.0.0.1:11434` in Python development or a Docker-reachable address in containers. Host localhost inside a container refers to that container. Apply `OLLAMA_NO_CLOUD=1` to host-managed Ollama separately. Generation and embedding providers can be mixed; selecting Ollama for either requires its configured endpoint and model. Refresh status after pulling. Grimoire does not start a host daemon or pull weights automatically.

## Existing data and reindexing

Existing Ollama embeddings keep their stored model identity when `EMBEDDING_PROVIDER=ollama`. To preserve an existing index initially, select that provider explicitly. Generation can switch to native independently.

To switch embeddings to native or another model, delete indexed documents while running the old embedding configuration, stop the app, update configuration, restart and re-ingest originals. Storage refuses a mismatched embedding identity with existing documents; dimensions are also checked. Native identity includes provider, model identifier and configured path, so moving its path also requires reindexing. Native identity also includes size, nanosecond mtime, file identity and a bounded content fingerprint. Normal same-path replacement invalidates the index; deliberately preserving all metadata and modifying only unsampled bytes can evade this lightweight check. Legacy native identities require re-ingestion. Pin model revisions and deliberately reindex. Chunks are not a complete source archive; keep originals.

## Backup and restore

Stop the backend before copying SQLite data. Copy all of `data/runtime` for Python development or archive the stopped Compose data volume, including WAL/SHM files. Back up your model host directory separately (and optional `ollama-models` volume if used). Qdrant rebuilds from persisted vectors. Verify actual volume names with `docker volume ls`; project-name overrides change them. Preserve permissions on restore and rehearse documents, search and inventory. Docker backup/restore is not validated here.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Not configured | Set the relevant native GGUF path |
| Model missing | Host file, container path, read-only mount and permissions |
| Loading | Wait and refresh; loading uses local CPU/RAM and does not download |
| Native runtime unavailable | Install native extras; inspect platform/compiler/DLL compatibility |
| GGUF loading failed | File format, compatible architecture/quantization, RAM and context |
| Chat ready, embeddings unavailable | Separate embedding path/provider; chat can still work |
| Generation/embedding 503 | State, context limits, compatible model/input conventions and output budget |
| Ollama unavailable | Optional endpoint/model availability; use native providers to remove this dependency |
| Upload 422 / request 413 | UTF-8/type/syntax/chunk/byte limits |
| No evidence | Search originals, inspect prefixes and model choice, evaluate retrieval threshold |
| Interrupted stream | Error event; partial answers are not complete |
| Invalid Host / 400 | Loopback hostname or explicitly configured authenticated ingress |

Logs exclude bodies, query strings, model output and native exception details. Match `X-Request-ID` to local JSON request logs. Compose forwards settings listed in its `environment`; other settings need explicit overrides.

## Dependency locks

`backend/requirements.lock` pins the base Linux/Python 3.12 install; `requirements-native.lock` additionally pins the native extra used in Docker. Regenerate deliberately with:

```bash
uv pip compile backend/pyproject.toml --python-version 3.12 --python-platform linux -o backend/requirements.lock
uv pip compile backend/pyproject.toml --extra native --python-version 3.12 --python-platform linux -o backend/requirements-native.lock
```

Test/build after updates. Dev tools use constrained ranges. Base images use version tags; digest pinning/scanning remain release-hardening work.

## Offline runtime

Initial acquisition needs internet access (or an offline package/image transfer): Python packages, container base images and your explicitly chosen model weights. Normal native runtime uses local files, SQLite and in-process Qdrant. It does not fetch missing weights, use a CDN, call a cloud AI endpoint or enable telemetry exporters. Optional Ollama must also be pre-provisioned and reachable locally.

After provisioning, run the [demo](demo.md) with outbound access denied in a disposable deployment. Do not disconnect a shared host just to test this. Check startup, assets, both models, ingestion, answers/sources, search, logs, incidents and inventory, then restart and repeat retrieval. Weightless startup and the synthetic-provider pipeline have network-denied automated coverage; a disconnected deployment with real models has not been verified. See [exact results](validation.md).


## Local reliability evaluation

Use the existing provisioned models and ordinary `GRIMOIRE_` environment settings:

```powershell
. ./.env.native.ps1
.venv/Scripts/grimoire.exe evaluate
.venv/Scripts/grimoire.exe evaluate --json --output .cache/reliability/results.json
.venv/Scripts/grimoire.exe evaluate --retrieval-only --json
.venv/Scripts/grimoire.exe evaluate --compare-legacy --limit-probe --output .cache/reliability/comparison.json
```

The suite is packaged in `app/evaluation/suite.json`; no private infrastructure data is read. Each run uses a disposable temporary SQLite/Qdrant index and leaves the configured knowledge index untouched. No weights are downloaded. Retrieval-only loads embeddings; other runs load the configured generation model too. Progress goes to stderr; JSON stdout is a single result. Optional saved reports contain synthetic text, ranks, line ranges, expected behaviour, required evidence, generated selections, oracle-evidence answers, latency and token/termination metadata.

The default runs production retrieval and hardened knowledge/log generation. Oracle-evidence runs isolate generation from search. `--compare-legacy` additionally exercises the previous prose prompts; those outputs require manual review. `--limit-probe` compares old log prose at 384 and 768 tokens without changing application configuration. There is no overall accuracy score. Fact-marker checks are narrow assertions, not a semantic judge.

Exit 0 means explicit checks passed (WARN remains visible); exit 1 means a required top-5 miss, a failed case, or an operational error. Hit@1/3 remain diagnostics, not mandatory gates. Configuration errors exit 2. These commands can take several minutes on CPU. Ordinary pytest tests use fakes and require no GGUFs. See [real-model results](real-model-acceptance.md#reliability-milestone) for limitations and the evaluated baseline.
