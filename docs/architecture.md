# Architecture and decisions

Grimoire v0.1 is an operator-driven local knowledge tool. It reasons over supplied documentation and log excerpts. It does not inspect running services or execute commands. `create_app(settings, provider, embeddings)` is the composition boundary: tests replace the provider without changing API logic.

## Request paths

- Chat → validation → bounded retrieval/context → provider → answer/stream + source disclosures.
- Upload → byte limit → sanitized basename → UTF-8 validation → syntax validation → line-aware chunks → batch embeddings → SQLite transaction + Qdrant index.
- Search → tokenized SQLite FTS5 query and SQL metadata filters; inventory search examines stored records. No model call.
- Logs → deterministic severity/repetition evidence → optional bounded explanation → JSON or Markdown.

Optional Ollama HTTPX calls are asynchronous. Native model loading, generation, stream iteration/cleanup and embeddings run on dedicated worker threads. Async endpoints offload parsing and persistence to worker threads. A lock serializes vector queries/mutations; SQLite connections are short-lived with foreign keys enabled. Two inference slots and one ingestion slot bound active work locally; these are not a distributed queue or internet-facing rate limit.

## ADR 001: SQLite instead of PostgreSQL

One operator and one process do not justify another database service. SQLite provides transactions, WAL, document/service persistence and full-text search. PostgreSQL becomes useful with concurrent writers, accounts or multi-process execution. The initial schema bootstrap is not a migration framework.

## ADR 002: Embedded Qdrant, durable SQLite embeddings

Qdrant local mode supplies cosine vector search without a separate daemon. SQLite stores chunks and vector arrays durably; an in-memory Qdrant collection is rebuilt on startup. This provides one authoritative backup boundary instead of two independently persisted stores.

Ingestion stages SQL changes, upserts vectors under the storage lock, then commits SQL. On failure, points are removed and SQL rolls back. A crash before commit leaves no durable document; restart reconstructs consistent index state. Deletion commits SQL before point cleanup. Every vector hit joins current SQL rows, so deleted text cannot be returned from stale points.

This trades RAM and startup time for straightforward local operations. Use exactly one backend worker. Scale via server-backed Qdrant, durable indexing jobs and reconciliation. An embedding-model identifier is persisted; changing it with indexed documents prevents startup. Dimensions are checked too. Updates under the same model name/dimension cannot be detected: pin your model versions and re-ingest deliberately after updates.

## ADR 003: Provider protocol

`app/ai/base.py` defines separate generation (`ModelProvider`: chat, stream) and embedding (`EmbeddingProvider`: embed) protocols. Both expose startup, lightweight health/identity and close. `app/ai/registry.py` selects adapters independently from validated settings. API, RAG and analysis code have no Ollama-specific calls. RAG receives an embedding provider, and sends assembled evidence to the same generation provider used by general chat and logs/incidents.

Native inference is the default: Grimoire → NativeProvider → llama-cpp-python → local generation GGUF. A separate NativeEmbeddings instance loads a pooled embedding GGUF with `embedding=True`; it does not embed using the chat model. Both retain one loaded instance, use configurable context/threads/GPU layers and never download weights. Optional Ollama adapters retain `/api/chat`, `/api/embed` and tags-only readiness; embedding truncation is disabled and vectors are validated before storage.

Startup schedules native loads in the background. `not_configured`, `model_missing`, `loading`, `ready` and `failed` describe each subsystem independently. `/health` is liveness; `/api/status` includes `ai` and `embeddings` metadata (provider, backend, basename/model, state, reason and context where applicable). Overall readiness needs both models; general chat works with generation alone. Health never invokes generation or loads a model. Invalid providers/options fail settings validation; missing files/bindings and load failures leave the app degraded with safe public errors. Restart after repairing configuration/files; there is no hot reload.

Each native instance owns a single-thread executor and an async lock spanning a complete request or stream. All binding calls, including load/next/close, stay on that worker. Generation serializes across chat, RAG, log and incident requests; independent embeddings can run concurrently. Cancellation drains the active native call before releasing ownership; token evaluation or load cannot be forcibly interrupted. Stream disconnect cleanup closes its iterator. CPU work may continue until the current call finishes, shutdown waits for loading/inference, and no native hard timeout is promised. Two API inference slots and one ingestion slot remain; use one process/worker to avoid model duplication and storage races.

SQLite preserves the previous Ollama embedding identifier for compatibility. Native identity includes provider, identifier and model path; switching embedding provider/model/path with indexed documents refuses startup. Delete documents under the old configuration, then restart and re-ingest. Changes to weights at the same path/name are undetectable: pin versions and re-ingest deliberately. Generation model changes do not invalidate embeddings. Parsing, SQLite/Qdrant persistence, source assembly and log heuristics are unchanged.

Native public errors/log events exclude exception strings, full paths and prompts. The binding uses `verbose=False`. A generation error (such as context overflow) returns an error without unloading an otherwise loaded model. Character budgets remain approximate; choose a compatible context and leave output room.

## ADR 004: Bounded evidence

Knowledge chat retrieves up to six above-threshold chunks and assembles JSON excerpts within a character budget. Request-local source labels map to original filenames, paths and line ranges. Previous assistant claims are excluded from knowledge context; the last user question gives limited follow-up context. No adequate retrieval means no generation call.

This assists verification; it does not prove answers. Similarity thresholds need corpus-specific evaluation. Models can ignore instructions or invent citation labels. Character budgets approximate model context, not exact token counts.

## ADR 005: Line-preserving parsing

Headings/fences, paragraphs, configuration keys and log-entry boundaries guide chunking. Oversized lines are split as a bounded exception. Syntax validation does not rewrite documents, keeping line references meaningful. YAML event parsing avoids object construction/alias expansion and rejects custom tags. AST hierarchy and inherited parent headings remain future improvements.

## ADR 006: Static frontend

HTML/CSS and JavaScript modules cover the four workflows without a framework or build pipeline. Untrusted content uses DOM text nodes. Responses/Markdown are displayed as text; incidents can be downloaded. Nginx provides a same-origin proxy with stream buffering disabled. The native development script serves the same assets with the API.

## Future integration boundary

Add typed read-only evidence providers with explicit source/time metadata, timeouts and permissions. Add durable jobs before automatic ingestion and authentication before multiple users. Document-suggested commands must not become executable tools.

## References

- [Qdrant Python client local mode](https://github.com/qdrant/qdrant-client#local-mode)
- [llama-cpp-python API and build options](https://llama-cpp-python.readthedocs.io/en/latest/)
- [Ollama API specification](https://github.com/ollama/ollama/blob/main/docs/openapi.yaml)
