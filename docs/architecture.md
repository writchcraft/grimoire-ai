# Architecture and decisions

Grimoire v0.1 is an operator-driven local knowledge tool. It reasons over supplied documentation and log excerpts. It does not inspect running services or execute commands. `create_app(settings, provider, embeddings)` is the composition boundary: tests replace the provider without changing API logic.

## Request paths

```mermaid
flowchart TD
    Browser --> Frontend[Static frontend · same-origin requests]
    Frontend --> API[FastAPI · validation / bounded work]
    API --> Features[Chat / RAG / log and incident analysis]
    Features --> Generation[Generation provider]
    Generation --> Native[llama-cpp-python · generation GGUF]
    Generation -. optional .-> Ollama[Local Ollama endpoint]
    API --> Ingestion[Parse / line-aware chunking]
    Ingestion --> Embeddings[Separate embedding provider / model]
    Features -->|query embedding| Embeddings
    Embeddings --> EmbedGGUF[llama-cpp-python · pooled embedding GGUF]
    Embeddings -. optional .-> Ollama
    Ingestion --> SQLite[(SQLite · text / vectors / metadata)]
    SQLite -->|startup rebuild| Qdrant[In-memory Qdrant · vector search]
    Features -->|retrieve evidence| Qdrant
    API --> Search[FTS search / service inventory]
    Search --> SQLite
```

Generation produces text; embeddings produce retrieval vectors. llama.cpp and Ollama are runtimes, not model weights. Qdrant ranks vectors; it does not generate answers. Logs/incidents extract deterministic evidence before optional generation. Inventory lives in SQLite and is searchable, but is not automatically included in RAG.

Native GGUF support keeps model loading and lifecycle within Grimoire, with explicit local files and no required model-server daemon. Ollama remains an optional adapter for operators who already manage models there. Neither runtime downloads weights on Grimoire's behalf.

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

This trades RAM and startup time for straightforward local operations. Use exactly one backend worker and stop it before running independent CLI writers. An embedding-model identifier is persisted; changing it with indexed documents prevents startup. Dimensions are checked too. Native weights use a full content SHA-256 independent of host paths. The optional Ollama adapter cannot detect weight updates behind an unchanged server model name; pin versions and re-ingest deliberately.

## ADR 003: Provider protocol

`app/ai/base.py` defines separate generation (`ModelProvider`: chat, stream) and embedding (`EmbeddingProvider`: embed) protocols. Both expose startup, lightweight health/identity and close. `app/ai/registry.py` selects adapters independently from validated settings. API, RAG and analysis code have no Ollama-specific calls. RAG receives an embedding provider, and sends assembled evidence to the same generation provider used by general chat and logs/incidents.

Native inference is the default: Grimoire → NativeProvider → llama-cpp-python → local generation GGUF. A separate NativeEmbeddings instance loads a pooled embedding GGUF with `embedding=True`; it does not embed using the chat model. Both retain one loaded instance, use configurable context/threads/GPU layers and never download weights. Optional Ollama adapters retain `/api/chat`, `/api/embed` and tags-only readiness; embedding truncation is disabled and vectors are validated before storage.

Embedding startup schedules native loading in the background; generation loads lazily on its first request. `not_loaded`, `not_configured`, `model_missing`, `loading`, `ready` and `failed` describe each subsystem independently. `/health` is liveness; `/api/status` includes `ai` and `embeddings` metadata (provider, backend, basename/model, state, reason and context where applicable). Overall readiness needs both models; general chat works with generation alone. Health never invokes generation or loads a model. Invalid providers/options fail settings validation; missing files/bindings and load failures leave the app degraded with safe public errors. Restart after repairing configuration/files; there is no hot reload.

Each native instance owns a single-thread executor and an async lock spanning a complete request or stream. All binding calls, including load/next/close, stay on that worker. Generation serializes across chat, RAG, log and incident requests; independent embeddings can run concurrently. Cancellation drains the active native call before releasing ownership; token evaluation or load cannot be forcibly interrupted. Stream disconnect cleanup closes its iterator. CPU work may continue until the current call finishes, shutdown waits for loading/inference, and no native hard timeout is promised. Two API inference slots and one ingestion slot remain; use one process/worker to avoid model duplication and storage races.

SQLite preserves the previous Ollama embedding identifier for compatibility. Native identity uses provider, configured identifier and full weight SHA-256; moving unchanged weights between machines preserves identity. Changed weights/identifier with indexed documents refuse startup. Legacy native fingerprints require re-ingestion into a new data directory; preserve source files and the old database before upgrade. Generation model changes do not invalidate embeddings. See [deployment compatibility](deployment.md#compatibility).

## Deployment persistence boundary

Schema version 1 adds original upload bytes and generated incident records to SQLite. New originals commit with document metadata, chunks, FTS and persisted vectors; service inventory is also authoritative SQL state. There are no mutable per-document paths outside the configured data directory. Model weights and configuration are separate operator-provisioned files. The data directory defaults to the existing local development location and can be absolute on Linux.

Operational backup uses SQLite's online snapshot API and a checksummed, versioned manifest. Restore validates storage and publishes only into a new directory. Original bytes unavailable for older documents cannot be reconstructed from chunks. Unknown schemas and malformed/mixed-dimension vectors fail clearly. `/ready` separately probes generation, embeddings and readable storage without inference; `/health` stays lightweight liveness. See [deployment and recovery](deployment.md) for the full contract and [actual acceptance](deployment-report.md) for platform limits.

Native public errors/log events exclude exception strings, full paths and prompts. The binding uses `verbose=False`. A generation error (such as context overflow) returns an error without unloading an otherwise loaded model. Character budgets remain approximate; choose a compatible context and leave output room.

## ADR 004: Bounded evidence

Knowledge chat merges up to 48 vector candidates with bounded SQLite FTS5/BM25 candidates for the question and up to four deterministic clauses. A small explicit scorer combines cosine similarity, normalized token/identifier overlap, entity/attribute coverage, inherited headings, filename/service metadata and scope/history/instruction-text penalties. Lexical evidence can rescue a chunk below the cosine threshold. The top 12 deduplicated candidates feed a separate evidence selector; they are not concatenated directly.

The selector ranks each clause independently, covers recognized attributes, and may include previous/next Markdown chunks from the same heading section. Expansion is bounded to immediate siblings, a 4,000-character anchor/sibling pair and eight selected/expanded records. Near-duplicates with different normalized terms, exact values, negations or query identifiers survive. A soft source-diversity preference applies after two results only within a 0.15 score window; clearly stronger sources retain priority. Selected anchors appear in relevance order before supplemental siblings.

JSON evidence packets preserve exact source text and line ranges, with headings/service identities as separate metadata. Duplicate excerpts are removed and complete records must fit the character budget. Empty evidence prevents generation. Independently supported parts survive a missing attribute; the answer contract marks the remaining slots unknown. The previous all-or-nothing attribute gate discarded useful partial evidence and has been corrected. These lexical checks are necessary signals, not proof of semantic sufficiency or a general question parser. Request-local labels retain source identity. Previous assistant claims are excluded; follow-up retrieval uses bounded prior-user context.

`grimoire knowledge search "query" --debug [--service NAME] [--json]` runs the same hybrid ranker without generation and reports scores/components, filters, identified needs and selected IDs. It now requires the configured embedding runtime. The browser's document explorer remains its existing lexical/filter API; supported QA uses hybrid search.

Knowledge generation selects application-owned fact candidates through a per-request JSON schema. Each recognized answer slot is a required key whose array can contain only that slot's candidate IDs; an empty array explicitly marks an unknown. Candidates contain field, whole-line value, source label, exact quote, heading scope and source line. The existing planner supplies fields; change questions also include documented host/container sibling ports. Grimoire rejects missing/duplicate slots, invalid slot/candidate combinations, unknown labels, invented excerpts, partial-line quotes (which could drop a negation), unsupported values and extra fields. The application assembles text and citation cards only from validated selections, returning the selected source subset and a claim-to-label map. Knowledge streams are buffered before answer text is emitted. General chat remains unvalidated prose. One constrained extraction call is followed by deterministic rendering, with no second generation call or automatic retry. Internal results record facts, unknowns, unresolved multiple records and extraction/composition/total timings. Original source-label/quote selection remains available only as the evaluation baseline (`direct_answer`); application knowledge requests use `facts.py`. One constrained extraction call is followed by deterministic rendering, with no second generation call or automatic retry. Internal results record facts, unknowns, unresolved multiple records and extraction/composition/total timings. Original source-label/quote selection remains available only as the evaluation baseline (`direct_answer`); application knowledge requests use `facts.py`.

This establishes excerpt provenance, not semantic correctness. A model can select irrelevant, outdated or misleading lines. Accepted slots retain alternative candidate records in separate source-bound rows, with a conservative multiple-record warning. This avoids synthesizing one configuration from distinct scopes but does not establish whether records genuinely conflict or are current. The evaluator reports rankings, required-evidence hit@1/3/5, context-budget misses and generation with oracle evidence separately. Character budgets approximate model context, not exact token counts.

Both generation adapters implement `chat_json` with constrained decoding and strict Pydantic validation. Native decoding omits string length/regex constraints that the bundled llama.cpp grammar cannot safely compile; Python enforces the full original contract. Output limits are task-specific: 512 knowledge, 384 log interpretation. General chat retains its configured limit. Native `finish_reason=length` and Ollama `done_reason=length` are failures, not successful answers. Evaluation records token/termination metadata without retaining private application prompts or answers.

## ADR 005: Line-preserving parsing

Chunks pack intact paragraphs, lists, fenced blocks and config/service groups where possible, retaining the 1,400-character target and 2,400-character hard bound. Markdown headings and TXT INI sections start new chunks; inherited heading paths are persisted separately from exact excerpts. YAML/JSON `services`/`containers` peer blocks are kept together below the hard limit; ordinary key/value groups remain intact. Timestamp/severity log entries retain continuation lines when bounded. Oversized units fall back to lines; oversized single lines use bounded fragments with their original line number. Syntax validation does not rewrite documents. This remains a lightweight text packer, not a complete AST chunker: unusually large config objects, deeply nested structures, exotic formatting and minified JSON can still split. YAML event parsing avoids object construction/alias expansion and rejects custom tags.

SQLite adds a default-empty `heading` column for existing indexes; existing vectors/text remain valid but do not gain new chunk boundaries/headings automatically. To apply the new packer to existing documents, deliberately delete/re-ingest them with the same embedding identity. A duplicate upload alone remains idempotent. No automatic model selection or index rewrite occurs.

## ADR 006: Static frontend

HTML/CSS and JavaScript modules cover the four workflows without a framework or build pipeline. Untrusted content uses DOM text nodes. Responses/Markdown are displayed as text; incidents can be downloaded. Nginx provides a same-origin proxy with stream buffering disabled. The native development script serves the same assets with the API.

## Future integration boundary

Add typed read-only evidence providers with explicit source/time metadata, timeouts and permissions. Add durable jobs before automatic ingestion and authentication before multiple users. Document-suggested commands must not become executable tools.

## References

- [Qdrant Python client local mode](https://github.com/qdrant/qdrant-client#local-mode)
- [llama-cpp-python API and build options](https://llama-cpp-python.readthedocs.io/en/latest/)
- [Ollama API specification](https://github.com/ollama/ollama/blob/main/docs/openapi.yaml)


## Structured log evidence and incidents

The application extracts line numbers, literal raw lines, heuristic severities, observed timestamps and repeated patterns. Named counters can be normalized for repetition; error codes, addresses and ports remain distinct. The model sees at most 12 whole events within a 5,000-character serialized event budget and bounded operator notes. `model_event_lines` discloses which observations reached generation.

The model selects typed symptom interpretations with supporting line references. Application-derived candidates reduce copying/counting errors. Validation rejects nonexistent references, unsupported symptom kinds, unmatched symptom text, restart claims without two separate restart lines, fabricated fields and numeric confidence. The renderer owns cautious descriptions (`possible`, `probable`, `insufficient evidence`) and read-only investigation suggestions. This narrow vocabulary sacrifices rich narrative detail for traceability. Other errors can return unknown.

Incident Markdown separates Confirmed, Probable and Unknown. Its relevant-event timeline is rendered only from extracted warning/error lines, never model text; notes remain labelled operator reports. It is a filtered input-order timeline, capped at 80, not a complete service history or verified root cause. Root cause and recovery remain unknown under this contract. No model-generated command is executed.


## Qualified evidence after ranking

Hybrid ranking keeps its existing scoring and query planner. The separate
`rag/evidence.py` answer planner describes requested attributes, subjects and
scopes for coverage selection and fact extraction. Each independent slot gets
priority before redundant evidence; source-bound qualified blocks replace whole
chunks when the context budget requires compaction. Deduplication includes
heading, lineage, service and provenance, so equal text in different scopes is
not silently merged. No new model or routing policy is involved.

Mapping/list records keep their peer properties, multiword keys and parent
heading hierarchy. Ancestor key paths and service/environment/role labels carry
original line references in a separate `lineage` metadata field across large
chunk splits. SQLite adds that field without rewriting existing text or vectors.
Existing long structured documents need reingestion to populate newly available
lineage metadata; the application does not silently rewrite the configured index.

Fact records keep exact quote/value text, source, line range, scope labels,
qualifier text, status and polarity. Status is one of asserted, negated,
historical, superseded, unknown, disproved or hypothetical. Unknown/disproved/
hypothetical records never become affirmative extraction candidates. Historical
values require a historical request; previous-only requests exclude current
records. An unknown-only packet returns a deterministic insufficient-evidence
answer without invoking generation.

The model selects eligible IDs; accepting one does not accept every lexical
candidate. A conservative guard can recover an empty selection only from an
explicit qualified port, subnet or retention value already in the packet. It
preserves original negation and validates the full source span. Fact output
records whether selection came from the model or this typed-value guard.
Quotes use the same 2400-character bound as ordinary ingestion blocks.

Relationship classification compares recognized values within explicit scopes,
not differences in prose or filenames. Equivalent facts are not conflicts,
missing scope does not create an alternative, and a negative constraint on one
port can coexist with use of another. Same-scope current contradictions,
different-scope alternatives and historical replacements have separate labels.
Uncomparable prose remains quoted evidence without a fabricated conflict label.
This is a bounded evidence representation, not a semantic knowledge graph.

## ADR 007: Deterministic generation selection

`ai/catalog.py` owns the validated generation inventory and simple routing configuration. `ai/registry.py` remains the execution-provider factory. `ai/routing.py` owns deterministic selection, per-request route metadata and serialized lazy runtime ownership. It converts a selected entry to provider settings, checks readiness, and tries only preferred / configured fallbacks / default, once per distinct candidate. No LLM, autonomous planner, cloud fallback, download or tool execution participates.

HTTP/CLI/evaluation generation use this routing boundary. The evaluation harness also accepts an explicit generation runtime for controlled fixed-vs-policy comparisons and model-independent tests. Task categories come from endpoint/mode metadata or an explicit general-chat task declaration. Indexed QA is always `rag`. Model choice never changes preparation, structured factual extraction, slot/source/scope/status validation, citation assembly, unsupported-answer handling or log/incident validation.

Generation is now lazy rather than background-loaded at application startup; embeddings retain their independent startup and index identity. Status distinguishes `not_loaded` from verified `ready`. One serialized generation owner closes the previous runtime before switching; it holds ownership through streaming and drains native cancellation. Ollama generation requires an explicit unload acknowledgement before switching; daemon-global memory remains outside process ownership. The light default and ordered policy/fallback metadata express resource preferences; there is no scheduler. See [model configuration](models.md) and [routing results](routing/report.md).
# Windows host evidence

The dedicated `HostProvider` protocol and `WindowsHostProvider` expose point-in-time facts through
allowlisted read APIs. Shared code creates `host_observation` and `windows_service_observation`
records, applies configured thresholds, retains 50 explicit inspection pages in authoritative
SQLite and correlates exact host/service identifiers and observation timestamps. Host evidence is
appended after incident model analysis and never enters model prompts or the vector store. The
GET-only host router, CLI and compact UI share these functions. See [host inspections](host-inspection.md).
