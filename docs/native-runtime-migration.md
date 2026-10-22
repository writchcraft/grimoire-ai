# Native runtime migration report

Grimoire defaults to in-process local GGUF inference via llama-cpp-python. Ollama remains an explicitly selected compatibility option. No cloud inference or automatic weight downloads were introduced.

## Files changed

Paths below are relative to repository root. The workspace was entirely untracked at the start; these are files edited/created for this migration, not a Git diff against a baseline.

| Area | Files |
| --- | --- |
| Provider layer (new) | `backend/app/ai/__init__.py`, `base.py`, `registry.py`, `native.py`, `ollama.py`, `embeddings.py` |
| Removed combined adapter | `backend/app/services/provider.py` (replaced by the AI modules above) |
| Integration | `backend/app/main.py`, `backend/app/core/config.py`, `backend/app/api/routes.py`, `backend/app/rag/retrieval.py`, `backend/app/ingestion/pipeline.py`, `backend/app/services/log_analysis.py` |
| Dependencies/container | `backend/pyproject.toml`, `backend/requirements-native.lock`, `backend/Dockerfile`, `backend/.dockerignore`, `docker-compose.yml` |
| Settings/weights exclusion | `.env.example`, `.gitignore` |
| UI | `frontend/index.html`, `frontend/src/app.js`, `frontend/src/api.js` |
| Tests | `backend/tests/conftest.py`, `backend/tests/test_api.py`, `backend/tests/test_provider.py`, `backend/tests/test_native.py`, `frontend/tests/api.test.js` |
| Documentation | `README.md`, `docs/architecture.md`, `docs/setup.md`, `docs/security.md`, `docs/validation.md`, this report |

## Architecture and lifecycle

Separate generation and embedding protocols keep inference out of API/RAG/log-specific logic. The registry independently selects native or Ollama adapters. Native instances load once on startup in background single-thread executors, serialize access for the full inference/stream lifetime and close at shutdown. Stream cleanup closes the iterator on disconnect. Public lifecycle is not configured, missing, loading, ready or failed, with safe reasons and provider/model/backend/context metadata. Health never generates text or loads models. No reload/retry control is implemented; restart after repairs. General chat works with generation alone; overall readiness and knowledge ingestion/retrieval need embeddings too.

## Added configuration

All settings have the `GRIMOIRE_` prefix: `AI_PROVIDER`, `EMBEDDING_PROVIDER`, `NATIVE_MODEL_PATH`, `NATIVE_EMBEDDING_MODEL_PATH`, `NATIVE_CONTEXT_SIZE`, `NATIVE_EMBEDDING_CONTEXT_SIZE`, `NATIVE_GPU_LAYERS`, `NATIVE_THREADS`, `NATIVE_CHAT_FORMAT`, `MAX_OUTPUT_TOKENS`. Compose additionally uses `MODELS_DIR` for its host mount. Existing Ollama URL/model settings remain compatible.

Generation and embedding models are separate GGUF instances. Native embeddings require a compatible pooled model; input-prefix conventions are operator-managed. Changing embedding provider/model/path requires deleting documents under the previous configuration and re-ingesting originals. Existing Ollama model identities are preserved for operators who explicitly select that embedding provider.

## Docker

The CPU image installs the locked native extra with build prerequisites. Host model storage is persistent and mounted read-only at `/models`; weights are not copied into the image or committed. Ollama has an optional Compose profile and no required dependency edge. The data volume and loopback frontend publication remain. GPU needs a customized compatible binding/image and device configuration.

## Results and limits

89 backend tests and 4 frontend tests pass; Python lint/format and frontend syntax checks pass. Compose structure was checked with a YAML parser. The native dependency lock resolved successfully. See [validation](validation.md) for exact scope.

Native tests use a fake binding; no real GGUF inference, binding build or Docker execution has been validated here. Native calls cannot be forcibly interrupted; cancellation/shutdown can wait for active work. Use one backend worker. CPU/GPU performance, actual model compatibility and quality are not established. No weights were downloaded.

Ollama-specific inference remains only in the optional AI adapters, settings, compatibility tests and Compose profile. The application factory retains one compatibility branch to preserve legacy Ollama embedding identities in SQLite. Feature code and frontend status have no Ollama-specific inference paths.

Recommended next milestone: Linux CPU Docker and real-GGUF acceptance, followed by measured retrieval/citation quality, cancellation/resource tests, updated screenshots and backup/restore rehearsal. Add model-specific embedding prefix support if acceptance shows it is needed.
