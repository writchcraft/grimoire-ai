# Validation record

28 October 2026, Windows / Python 3.12. Native-provider migration validation.

## Executed checks

- Full backend suite after repository organization: `python -m pytest backend/tests -q` — **121 passed** (89 existing plus 32 additional cases). One existing Starlette warning deprecates its HTTPX-based TestClient.
- Python: `ruff check backend scripts` and `ruff format --check backend scripts` passed.
- Frontend: `npm run check --prefix frontend` passed; `npm test --prefix frontend` — **4 passed**.
- Native dependency lock resolved from PyPI for Linux/Python 3.12, including llama-cpp-python 0.3.36. This resolves metadata; it does not establish a successful binding installation/build.
- Compose YAML parsed and structure checked: native default, read-only model mount, optional Ollama profile, no backend dependency on Ollama, loopback frontend publication. Docker CLI is unavailable, so Compose config/build/start were not executed.

Provider tests cover selection, invalid settings, unconfigured/missing files, loading/ready/failed states, safe errors, one-time load/reuse/close, worker-thread affinity, event-loop responsiveness, cancellation/draining, stream cleanup, incomplete/empty streams and separate embedding-vector validation. An API integration test exercises native adapters with a fake binding through general chat, ingestion, streamed RAG, logs and incident summaries. Existing parsing, storage, retrieval, persistence, service inventory, request validation and Ollama transport tests remain passing. Frontend tests cover NDJSON/UTF-8 decoding, provider errors, provider identity, lifecycle states and independently unavailable embeddings.

All generation/embedding results in these tests come from fakes or HTTPX mock transports. No real GGUF was loaded, and no successful real native inference is claimed. No model weights were downloaded.

Repository organization adds coverage for ignored credentials, environment files,
model weights, databases, caches and generated assets, while retaining public
examples. Additional tests reject unsafe configuration, exercise both mixed
provider combinations, and start the real application adapters without weights
to verify static frontend delivery, liveness, degraded status, offline search,
deterministic log analysis and SQLite creation. The startup check uses the
in-process HTTP test client; it does not establish Docker or real-model readiness.

The Windows sandbox hangs creating asyncio's loopback socket pair before test code runs. Backend tests were run outside that sandbox; no application workaround was introduced. Restricted-network dependency resolution was also retried outside the sandbox.

## Historical UI validation

Before this migration, browser smoke checks exercised the development server with Ollama unavailable: insufficient evidence, deterministic logs/incidents, service persistence, Host/CSP checks and download controls. Existing screenshots contain fictional data and predate the new status labels. They were not recaptured during this migration. The report download link was observed previously; its downloaded file was not separately inspected through the browser.

## Unverified locally

Real llama-cpp-python installation, actual GGUF loading, model-specific templates/pooling/prefix conventions, response quality, native cancellation latency, GPU acceleration, Linux Docker build/runtime/permissions, large-corpus behavior and backup/restore remain unverified. Python 3.13 checks are defined in CI but were not run locally.

## Next acceptance run

On a Linux Docker host, explicitly supply compatible generation and pooled embedding GGUF files, build/start without Ollama and verify status transitions. Index fictional examples, check citations against expected ports/addresses, test unanswerable questions, interrupt a stream and make another request, exercise log/incident explanations, restart and verify persistence, delete documents and check both searches, then rehearse stopped-volume backup/restore. Measure RAM/latency instead of assuming performance. Repeat using optional Ollama and mixed provider configurations. Capture updated screenshots with fictional data only.
