# Setup and operations

## Native GGUF inference (default)

Ollama is not required. Generation and embeddings load separate local GGUF files through llama-cpp-python. Grimoire never downloads weights during startup. Obtain compatible files explicitly, check their licenses and provenance, and retain them outside Git. A Qwen2.5 3B Instruct GGUF is an example chat model; use a pooled embedding GGUF for retrieval, not the chat model. No particular weights have been validated on this implementation host.

For Docker, copy `.env.example` to `.env`, create `models/`, and place generation weights at `models/chat.gguf` and embedding weights at `models/embed.gguf`. Set `GRIMOIRE_EMBEDDING_MODEL` to an identifier for your actual embedding model. Configure a different host directory with `GRIMOIRE_MODELS_DIR`; the backend sees it read-only at `/models`. Model paths in `.env` are container paths. Then run:

```bash
docker compose up -d --build
```

The frontend publishes only on loopback port 8080. The backend persists SQLite/embeddings in `grimoire-data`; model files persist in your host directory, not the image. Keep the directory readable by container UID/GID 10001. The CPU image includes build tools because llama-cpp-python may compile from source; builds may take time and need RAM. No CUDA installation is assumed. The optional Ollama service is not started by default.

For Python development, install Python 3.12+ and a native C/C++ toolchain if no suitable wheel is available. Linux generally needs a compiler and CMake; Windows source builds need a supported Visual Studio C++ toolchain or another toolchain supported by the binding. See the [binding installation instructions](https://llama-cpp-python.readthedocs.io/en/latest/). From repository root:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend/requirements-native.lock -e './backend[native,dev]'
$env:GRIMOIRE_AI_PROVIDER = 'native'
$env:GRIMOIRE_EMBEDDING_PROVIDER = 'native'
$env:GRIMOIRE_NATIVE_MODEL_PATH = 'C:\models\chat.gguf'
$env:GRIMOIRE_NATIVE_EMBEDDING_MODEL_PATH = 'C:\models\embed.gguf'
$env:GRIMOIRE_EMBEDDING_MODEL = 'my-pinned-embedding-model'
.venv\Scripts\python.exe scripts/dev.py
```

On Linux/macOS use your environment's Python/pip and `export GRIMOIRE_NATIVE_MODEL_PATH=/path/to/chat.gguf` (and the other variables above). The development script serves UI/API on loopback 8080 and reads shell variables; it does not load Compose's `.env`. API-only: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log`.

For tests or Ollama-only development, `pip install -r backend/requirements.lock -e './backend[dev]'` suffices; importing the application does not require the native binding. Starting without paths/bindings still allows status, service inventory, existing lexical search and deterministic log/incident analysis.

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

To switch embeddings to native or another model, delete indexed documents while running the old embedding configuration, stop the app, update configuration, restart and re-ingest originals. Storage refuses a mismatched embedding identity with existing documents; dimensions are also checked. Native identity includes provider, model identifier and configured path, so moving its path also requires reindexing. Replacing weights at the same path/identifier cannot be detected: pin model versions and deliberately reindex. Chunks are not a complete source archive; keep originals.

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
