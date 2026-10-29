# Security, privacy and trust boundaries

## Intended use

One trusted operator on loopback. There is no authentication, encryption at rest or user isolation. Anyone who reaches the API can search, upload and delete knowledge/inventory. Use authenticated TLS ingress before widening access. Keep backend/model ports private.

Default Compose publishes only `127.0.0.1:7864`. Trusted Host validation reduces DNS-rebinding exposure. Writes with an Origin header must match the Host; missing Origin is allowed for CLI clients. These checks are not authentication. CORS is disabled. Self-only CSP, anti-framing and referrer headers constrain the browser.

## Data flow

By default supplied content stays in the application process and local GGUF runtimes. Selecting optional Ollama sends inputs to the configured endpoint. Keep that endpoint local. HTTPX ignores ambient proxy environment variables. FastAPI telemetry is explicitly disabled; the frontend has no analytics, fonts, external assets or CDN documentation.

Installation downloads packages and images. Grimoire never downloads weights at startup; users explicitly supply them. Treat model files as trusted inputs to a native runtime. The supplied Ollama container sets `OLLAMA_NO_CLOUD=1` to disable cloud models/search, following the [official Ollama configuration](https://github.com/ollama/ollama/blob/v0.40.11/docs/faq.mdx#how-do-i-disable-ollamas-cloud-features). Apply this setting separately to a host-managed Ollama server. Configuring a remote endpoint intentionally changes where evidence goes. Grimoire cannot prove an endpoint stays local. Protect Ollama logs and process memory too.

## Upload boundary

- Bodies and file bytes have limits, including UTF-8 byte checks for JSON ingestion.
- Basenames/extensions are sanitized and checked; names and source paths are labels, not server paths to read or execute.
- Empty, binary-control and non-UTF-8 text is rejected. JSON/YAML syntax is validated; YAML objects are never constructed and aliases never expanded. Custom tags are rejected.
- No shell, SSH, Docker socket, dynamic import or infrastructure write tool exists. Model/parsing work still consumes resources; this is not a public upload service.
- Original bytes are not retained as files. Chunks, vectors, metadata and inventory persist in SQLite. Identical content/metadata is deduplicated; changed content creates a new document, so delete obsolete versions explicitly.

## Prompt injection and evidence

Documents/logs can instruct models to ignore rules, expose secrets, fabricate results or suggest dangerous commands. JSON evidence encoding, system instructions and excluding prior assistant claims reduce confusion but do not eliminate injection. Similarity is not truth. Models can invent source labels or claims. `grounded` only indicates supplied context.

Inspect source excerpts and verify commands and their effects manually. No indexed instruction can execute an infrastructure command because there is no execution path. Never index credentials, private keys, tokens or unredacted sensitive logs. Prompt instructions are not reliable redaction. Every application user can access all indexed material; embeddings require the same protection as text.

## Rendering

All untrusted UI text uses DOM text nodes, never `innerHTML`. Responses and reports display as plain text. Model-generated Markdown may contain unsafe links/HTML if rendered elsewhere: use a sanitizer and safe URL schemes. Inventory URLs only permit HTTP(S) without credentials and open with `noopener noreferrer`.

## Retention

Chat lives in the browser tab and is not persisted by Grimoire's backend. Application logs exclude bodies, query strings, model responses and provider exceptions. They include request IDs, route templates and timing. Generic unexpected-error events reduce leakage but limit debugging detail.

Document deletion removes SQL metadata/chunks/full-text rows/embeddings and vector points. Semantic hits must join SQL, so stale points cannot expose deleted text. Restart rebuilds the index. This is logical deletion, not secure erasure: SQLite free pages/WAL, memory and backups can retain data. Follow encrypted-volume and backup-retention procedures for sensitive disposal.

## Remaining hardening

Authentication/access scopes, migration tooling, corpus/queue quotas, image/dependency scanning, TLS ingress, resource stress testing, backup/restore rehearsal and model/citation evaluation. Future read-only integrations need explicit permission models; do not add an autonomous shell path.
