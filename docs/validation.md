# Validation record

> Current project decision — 29 October 2026: the Windows-native development/runtime milestone is **CLOSED**, with verification limited to the existing evidence in [the deployment report](deployment-report.md). Linux native and Docker deployment are intentionally **DEFERRED / NOT YET TESTED**, preserved as future release/deployment acceptance tasks. They do not block continued Windows development or a supervised first read-only homelab integration. No runtime discovery or installation is requested. All earlier BLOCKED statements below are historical observations, not current development blockers.

> Update — 29 October 2026: real CPU binding and both GGUFs are now provisioned and executed. See [current real-model acceptance](real-model-acceptance.md) for measured results, browser evidence, process-level offline checks and quality limitations. Earlier blocked entries below are historical. Docker remains **BLOCKED — Docker CLI unavailable**.

> Update — 29 October 2026: real CPU binding and both GGUFs are now provisioned and executed. See [current real-model acceptance](real-model-acceptance.md) for measured results, browser evidence, process-level offline checks and quality limitations. Earlier blocked entries below are historical. Docker remains **BLOCKED — Docker CLI unavailable**.

## Real local model acceptance attempt â€” 29 October 2026

**BLOCKED at model provisioning. This milestone is not complete.** No model weights were downloaded, converted, loaded or committed. No successful native inference, model-quality assessment or resource benchmark is claimed. Earlier synthetic and browser results below retain their original scope.

### Environment and acquisition boundary

Windows, Python **3.12.3**, repository `.venv`; no `GRIMOIRE_` environment overrides were present. `llama_cpp` was not discoverable in that interpreter. Generation and embedding paths were both unset. Effective defaults were native/native, generation context 8192, embedding context 2048, GPU layers 0 (CPU configured, not executed), threads/chat format unset, output cap 2048.

Local searches covered Documents and Downloads plus `.cache`, `.lmstudio`, `.ollama` and `~/models`. A recursive search found repository test GGUFs and llama.cpp tokenizer fixtures in the build cache; a follow-up traversal excluding caches, virtual environments, dependencies and junctions found no GGUF in Documents/Downloads. Standard model/cache locations contained no `.gguf`. This is a bounded search, not an assertion about every disk or other users' directories. No compatible generation/embedding pair was found. The Safetensors directory mentioned in the previous review was not converted or treated as usable GGUF.

Exact candidate files, explicit download instructions and a separate CPU acceptance configuration are now in [setup](setup.md#small-cpu-acceptance-setup-not-yet-verified): Qwen2.5-0.5B-Instruct Q5_K_M generation and All-MiniLM-L6-v2 F16 embeddings. These are **proposed, not used**. Their compatibility, pooling, template behavior and answer quality remain unverified. No acquisition was performed. No second inference path, feature redesign or automatic download mechanism was added; application code and the pre-existing working-tree changes were preserved.

### Executed checks

| Check | Result | Evidence / boundary |
| --- | --- | --- |
| Publication gate | **VERIFIED** | `.venv\Scripts\python.exe scripts/check.py`: **147 backend tests passed**, **4 frontend tests passed**, Ruff lint/format, JavaScript syntax, tracked artifact/credential-pattern audit and whitespace checks passed. One Starlette/HTTPX deprecation warning remains. Tests use fakes where inference is required. |
| CLI `status --json` | **VERIFIED** diagnostic behavior | Exit 0; `unverified/degraded`, both native models `not_configured`, `inference_verified=false`, binding absent. Report completion is not model readiness. |
| CLI `doctor --json` | **VERIFIED** diagnostic behavior | Exit 1; generation/embedding configuration and binding checks fail with setup instructions. Configured SQLite integrity/identity check passes; vector reconstruction is not executed. |
| CLI `models list --json` and `models inspect --json` | **VERIFIED** diagnostic behavior | Both exit 0; unset paths, no model filenames/sizes/header verification, neither model inference-verified. |
| CLI `models verify --json` | **BLOCKED** inference; diagnostic behavior **VERIFIED** | Exit 1; both roles explicitly report missing path configuration. No successful load or inference occurred. |
| CLI `validate --json` | **PARTIALLY VERIFIED** | Exit 1; application import and temporary synthetic offline ingestion/retrieval/no-evidence/persistence/vector reconstruction pass. Native configuration/binding are blocked. Configured index reconstruction and real offline inference remain unverified. |
| Docker | **BLOCKED â€” Docker CLI unavailable** | Executable discovery returned no Docker. No config command, builds, health checks, mounts, persistence or container runtime were executed. Compose source reviewed: separate model paths, read-only `/models`, CPU defaults, optional Ollama. |
| Model exclusion | **VERIFIED** current tracked-file audit | Gate found no tracked model weights; existing `models/` and `*.gguf` ignore rules retained. This is not an exhaustive Git history audit. |

Initial sandboxed Python runs stalled during asyncio initialization, consistent with the previously documented Windows restriction; those attempts were interrupted. The successful gate and CLI runs above executed outside that sandbox. CLI JSON/stderr evidence is in ignored `.cache/real-model-acceptance/`; it is local scratch evidence, not a tracked release artifact. No configured documents were ingested/deleted and the existing index was not rebuilt by these commands.

### Inspection findings and remaining acceptance

Inspection covered settings/path handling, native adapters/registry, shared application lifecycle, CLI/doctor/validate, parser/chunker, SQLite/Qdrant storage, retrieval/context assembly, log/incident analysis, API routes and frontend status/source rendering.

| Required outcome | Status and findings |
| --- | --- |
| Native generation, normal/stream/repeated requests, context/output limits | **BLOCKED** â€” weights/binding unavailable. Source and fake-binding tests exercise one model per provider, a serialized worker, configured context/output limits and cleanup. Real template handling, token limits, CPU execution and reuse remain unverified. |
| Independent real embeddings and stable dimensions | **BLOCKED** â€” no embedding weights/binding. Embeddings use a separate native instance and return normalized pooled vectors with truncation disabled; no real vectors or dimensions were measured. |
| Full RAG, exact/multi-document/unsupported/distractor queries | **BLOCKED** â€” real embeddings and generation unavailable. Synthetic integration results do not establish semantic relevance or safe refusal when related but insufficient evidence is retrieved. Resume with the existing fictional examples and setup walkthrough. |
| Citation faithfulness | **PARTIALLY VERIFIED** construction only â€” backend assigns labels, paths, excerpts and original line ranges from retrieved hits and sends these separately to the UI. Inline `[Sâ€¦]` use and factual prose are still model-controlled; `grounded=true` means sources exist, not that every claim is entailed. No real answer was available for manual claim/excerpt inspection; no real citation mismatch is asserted or ruled out. |
| Model-assisted log analysis | **BLOCKED** â€” no real explanation produced. Existing tests cover deterministic severity/repetition and fake model prompts. Ambiguous connection-refusal explanation still requires manual evidence/hypothesis review. |
| Model-assisted incident summary | **BLOCKED** â€” no real summary produced. Report fields and event timeline are constructed from supplied evidence; the explanation can still contain unsupported prose. Actual probable-cause/uncertainty/remediation quality and absence of invented claims remain unverified. |
| Real web workflow | **BLOCKED** â€” not run with real models. Source review confirms independent model status/reasons, streamed error handling and expandable retrieved citations. Historical weightless browser acceptance below is not real-model acceptance; no new browser run is claimed. |
| Real offline runtime | **BLOCKED** â€” prerequisites absent. Network-denied tests passed with synthetic providers/weightless adapters; no loaded-model disconnected startup/generation/embeddings/RAG/logs/incidents was tested. No OS network settings were changed. |
| Resource behavior | **UNVERIFIED** â€” no actual weight sizes, load times, process memory delta, inference latency or GPU execution observations. CPU is configuration only. No performance claim. |
| Real vector index/rebuild | **BLOCKED** â€” synthetic fresh-index/restart/delete/dimension safeguards passed in tests; configured index received only read-only integrity/identity inspection. Real re-ingestion, versioning, restart and deletion require weights. |
| Failure acceptance | **PARTIALLY VERIFIED** â€” CLI missing configuration/binding exercised directly. Missing/invalid files, load/inference errors, dimension mismatch, malformed input and empty retrieval have automated fake/weightless coverage in the passing suite. Corrupt real GGUF parsing, model loss during a real request and real API/UI failures remain unverified. |

Storage refuses changed embedding identity with existing documents, and vector dimensions are checked. Native identity currently includes model identifier and configured path, not a content hash. Replacing weights at the same path/identifier with the same dimension is **not detected**. Pin immutable files/revisions and change the identifier when weights change, using the documented deletion/re-ingestion procedure or a fresh data directory. Do not describe this as automatic detection of every incompatible model replacement.

Read-only native diagnostics do not remember prior `models verify` success: they report file/header/binding evidence, not live loaded state. Use `/api/status` for the running process and repeat requests there to verify reuse. `validate` does not perform real inference or rebuild the configured index. Its Docker row currently says UNVERIFIED; the operational reason in this record is **BLOCKED â€” Docker CLI unavailable**.

**Recommended next milestone:** explicitly provision the two candidate files and native binding, then resume this same real-model acceptance milestone. Record model revision/hash, dimensions/pooling, actual configuration, resource observations and manual answer/citation review before claiming success. Complete real browser, offline and configured-index acceptance; Docker can remain separately blocked. Do not add product features to substitute for this evidence.

## Portfolio review â€” 29 October 2026

Windows / Python 3.12. Checks below were executed during this review; historical results follow separately.

| Area | Result and boundary |
| --- | --- |
| Local publication gate | `python scripts/check.py`: **123 backend tests passed**, **4 frontend tests passed**, Ruff lint/format and JavaScript syntax passed; tracked artifact/credential-pattern audit and whitespace check passed. One existing Starlette/HTTPX TestClient deprecation warning remains. |
| Native inference | **Not verified with real weights.** Native binding is absent. Usual model directories and Documents/Downloads had no runnable GGUF; cached GGUFs were tokenizer fixtures. A subsequently supplied local folder held 26 BF16 Safetensors shards (about 67 GiB), not GGUF. The operator deferred model setup; it was left untouched. No model download, conversion or successful load is claimed. |
| Native lifecycle | Existing fake-binding tests passed for load/reuse, streaming, repeated calls, errors, worker ownership/cancellation, context/thread/GPU settings and missing files. These are contract tests, not CPU/GPU inference measurements. Model name, real load time, memory use and response latency are unavailable. |
| RAG plumbing | New `test_offline.py` uploads the unchanged monitoring Markdown, proxy YAML and database JSON fixtures through the real parser/chunker, SQLite and in-memory Qdrant. Synthetic vectors and canned generation verify stream framing, source labels, exact original line excerpts, prompt/context correspondence, deduplication, below-threshold refusal, restart/rebuild, inventory and deletion. **This does not measure real embedding relevance, citation faithfulness or related-but-unanswerable questions.** |
| Offline coverage | Those tests deny non-loopback socket connects and DNS lookups, recording unexpected attempts. Both the synthetic-provider pipeline and actual weightless native adapters passed without external attempts. The UI uses same-origin/local assets and code review found no default cloud AI/telemetry exporter. The workstation was not physically disconnected; disconnected browser/Docker/real-model operation remains unverified. |
| Docker | `docker compose config --quiet` could not execute: Docker command unavailable; standard Desktop executable also absent. Compose/CI YAML parsed and structural assertions passed for CPU/native defaults, optional Ollama profile, read-only model mount, private backend/model ports and loopback publication. Image builds, health checks, permissions, mounts, volumes, native runtime and container restarts **not executed**. |
| CI | Workflow reviewed: Python 3.12/3.13, frontend checks and Compose builds; tracked-file hygiene added. Local Python 3.12 checks passed. **Remote GitHub Actions and local Python 3.13 not verified.** |

### Browser observations

Started the real development server with isolated ignored data on loopback **7864**, without model paths. Opened Assistant, Knowledge, Logs & incidents and Services. Verified empty-evidence refusal, actionable missing-generation error, lexical search empty state, service creation and inventory search, deterministic log analysis (7 lines, 3 errors, 1 warning), and incident Markdown display with uncertainty. A stopped/restarted backend cleared stale status and recovered; the fictional service persisted. No successful ingestion/answer with real models or expanded live RAG citations could be inspected.

Refreshed all repository screenshots with native-provider labels and explicitly fictional service/incident data. At 1280Ã—720 the welcome cards now fit above the composer. A 390px viewport had no horizontal document overflow; exhaustive mobile/browser/accessibility testing was not performed. Temporary viewport overrides were reset. Clicking the browser incident download did not yield a completed download event before the browser-control timeout; the downloaded file was **not verified**. Markdown API generation and on-screen content passed.

### Changes supported by review

README now leads with infrastructure purpose, local data/evidence flow, concrete capabilities and stack, with setup before the detailed diagram and direct evaluation links. Architecture distinguishes generation, embeddings, runtimes and storage. The new demo includes expected facts and negative checks. Setup documents initial acquisition versus offline runtime and relative model paths. Default host port is 7864 for development and Compose; internal Nginx remains 8080.

UI guidance exposes safe provider failure reasons, backend outages clear stale model identity, upload copy accurately describes retained chunks rather than original files, and themed labels were simplified. Docker context exclusions were extended for environment files, nested bytecode, data/build artifacts and weights. The `/health` proxy now forwards Host consistently with API requests; this fix is source-reviewed, not container-tested. No application command-execution path was added or found in the reviewed production modules. Tracked-file checks found no credential-pattern hits; this is not an exhaustive secret/history scan.

### Recruiter assessment and release boundary

- **15 seconds:** title and opening identify a self-hosted infrastructure assistant, native GGUF and optional Ollama.
- **60 seconds:** capability table, stack and quick start establish scope and how to try it.
- **5 minutes:** architecture and security links explain local inference, separate embeddings and durable SQLite/in-memory Qdrant.
- **15 minutes:** code map, demo, substantive tests and this record expose implementation and verification limits. Docker execution and real-model acceptance still require evidence.

The review changes are suitable for a focused commit once the final diff is inspected; no commit or push was made. Do not present the project as real-model/Docker release-validated yet. No major features, integrations, authentication, autonomous execution, new databases or cloud services were added; the existing architecture and visual identity were retained. Model setup was deferred explicitly by the operator.

## Historical validation â€” 28 October 2026

28 October 2026, Windows / Python 3.12. Native-provider migration validation.

## Executed checks

- Full backend suite after repository organization: `python -m pytest backend/tests -q` â€” **121 passed** (89 existing plus 32 additional cases). One existing Starlette warning deprecates its HTTPX-based TestClient.
- Python: `ruff check backend scripts` and `ruff format --check backend scripts` passed.
- Frontend: `npm run check --prefix frontend` passed; `npm test --prefix frontend` â€” **4 passed**.
- Publication gate: `python scripts/check.py` passed after integration, including tracked-file credential-pattern/artifact checks and `git diff --check HEAD`. Only the public `.env.example` is tracked; local environments, model weights, runtime data and caches remain excluded.
- Live development smoke: started a temporary Uvicorn process on loopback with isolated temporary data and no model paths. Frontend HTML, JavaScript/CSS assets, liveness and inventory returned HTTP 200; provider status was degraded as expected. The process was stopped and temporary database removed afterward.
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


## Reliability milestone — 29 October 2026

The current measured record is [Reliability milestone](real-model-acceptance.md#reliability-milestone). Earlier missing-runtime/model and unverified-native statements in this file are historical and superseded by the subsequent real native acceptance and reliability runs.

The small development suite has 20 synthetic cases. Real MiniLM retrieval found all required evidence in 3/10 top-1, 8/10 top-3 and 9/10 top-5 results. Real Qwen 1.5B hardened QA passed 2/10 with retrieval and 7/10 with oracle documents. Unsupported questions passed 4/4 with retrieved context and 4/4 with oracle related documents. All five displayed excerpts were traceable, but two cited answers were incomplete. Structured log extraction/interpretation and deterministic incident checks passed 6/6 each. These are narrow evidence/structure checks, not general accuracy.

The full real-model command correctly exited 1. The normal automated gate passed separately: **187 weightless backend tests**, **four frontend tests**, frontend syntax, Ruff, diff whitespace and tracked-file hygiene. One existing Starlette/httpx deprecation warning remains. Regression coverage includes fabricated sources/excerpts, partial negations, malformed/truncated output, invented log/timeline fields, invalid event references, numeric confidence, error-code repetition, chunk/budget coverage, incomplete but traceable answers, isolated evaluation and CLI reporting. Real-model quality and manual prose review remain distinct from those automated tests. See the measured record for exact final counts, output limits, weaknesses and the decision against automatic model selection at this stage.


## Structured facts — 29 October 2026

The latest [structured fact acceptance record](real-model-acceptance.md#structured-fact-milestone--29-october-2026)
supersedes the earlier reliability scores above: unchanged-suite supported and
oracle QA both pass 10/10; retrieved/oracle unsupported refusals pass 4/4 each;
17/17 displayed excerpts are traceable; log extraction, interpretation and
incidents pass 6/6 each. Retrieval hit@3, hit@5 and complete packets remain 10/10.
The local gate passes 229 model-independent backend tests and four frontend
tests. Fixed evidence, full end-to-end evaluation, supplementary refusal results,
per-stage latency and limitations are linked in that record. No 3B structured
recheck, registry or automatic routing was introduced.


## Qualified evidence pipeline — 29 October 2026

See the [complete first-run comparison](heldout/after-general-fixes-report.md) and
[machine-readable audit](heldout/analysis-after-general-fixes.json). With the
same Qwen2.5-1.5B Q4_K_M model and unchanged frozen suites/scorer, complete
held-out packets improve from 17/22 to 22/22, supported QA from 14/22 to 22/22,
and oracle QA from 12/22 to 22/22. Unsupported refusals improve to 5/5 in both
modes; both adversarial cases pass. Development remains 10/10 supported/oracle,
4/4 refusals per mode, with all citations and logs/incidents passing. The full
gate passes 332 backend and four frontend tests. Routing readiness for the next
registry/policy milestone is YES on this diagnostic evidence; routing is not
implemented. The original held-out report, digest and classifications remain
immutable, and every completed follow-up run is retained separately.

## Deterministic generation routing - 29 October 2026

The registry/router use the existing evidence and validation services unchanged. Frozen development/held-out documents, expected outcomes and scoring assertions are retained. Routing tests exercise generation inventory validation, capability/readiness decisions, bounded fallback, explicit override, native ownership/switch-back, streaming serialization, cancellation draining, embedding identity isolation, and factual/log rejection through the actual HTTP handlers. Registry/CLI JSON and Auto/manual status labels are checked too.

Real fixed-vs-policy runs, a deliberately bounded 3B structured-log experiment and an actual 1.5B -> 3B -> 1.5B lifecycle/fallback probe are recorded in [the routing report](routing/report.md). These previously exposed suites are regression material, not a new blind held-out assessment. No benchmark outcomes are used as routing rules.

## Manual HTTP monitoring - 29 October 2026

Final regression: 426 backend tests and 5 frontend tests passed, including 37 new monitoring tests and all 389 prior backend tests. Ruff, changed Python formatting, JavaScript syntax and whitespace checks pass. Real local TCP API/CLI acceptance and a browser UI check also passed. See [monitoring report](monitoring-report.md) for evidence, limits, retention and the explicit separation between local acceptance and mocks. Linux/Docker remain deferred; real model quality evaluations were not rerun.

## Read-only DNS inspection - 29 October 2026

The [DNS acceptance report](dns-report.md) records 454 backend and six frontend tests
passing, including all existing HTTP monitoring and evidence-validation regressions.
The real development and held-out model suites were rerun successfully with the
provisioned local Qwen 1.5B and MiniLM weights; frozen suites/scorers were unchanged.
Real system-resolver acceptance covered localhost, a reserved invalid hostname and
a disposable configured local service. Browser checks verified resolved/unresolved
cards, hostname-only inventory and persistence after reload. No configured LAN
hostname existed. OS resolver semantics and raw-CNAME/TTL limitations are explicit;
HTTP SSRF validation remains independent. No infrastructure control was introduced.

TLS inspection acceptance and full regression results are recorded in [the TLS milestone report](tls-report.md), including disposable local certificate fixtures and unchanged development/held-out reliability suites.
