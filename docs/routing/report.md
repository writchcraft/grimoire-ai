# Model registry and deterministic routing milestone

29 October 2026. **Implemented; keep Auto effectively single-model.** Routing adds explicit configuration, controlled overrides, lifecycle safety and diagnostics. The measured stronger-model experiment gives no quality benefit on these regression tasks and increases latency/memory. This is deterministic dispatch, with no autonomous agents or model-selected routing.

## 1. Registry architecture

`backend/app/ai/catalog.py` defines a validated generation inventory, separate from provider factories in `ai/registry.py`. `ai/routing.py` owns deterministic selection and one lazy generation runtime. Existing native llama.cpp and optional Ollama adapters execute selected settings. No model IDs are hardcoded into application routing logic; Qwen IDs occur in example configuration and opt-in measurement scripts.

## 2. Configuration

Set `GRIMOIRE_MODEL_REGISTRY_PATH` to YAML/JSON. See [the complete schema guide](../models.md) and [example](../models.example.yaml). Unique model IDs carry display name, runtime provider, native path/Ollama name, capabilities, context/output limits, enabled state, light/medium/heavy resource metadata, tags and readiness. Relative paths resolve beside the registry. Existing environment-only configuration gets a compatibility entry. Embedding configuration and index identity remain unchanged.

## 3. Policies

Auto uses the configured default unless an explicit task mapping exists. The example uses light Qwen2.5 1.5B Q4_K_M for general, factual RAG, logs, incidents, configuration explanation/reasoning, code reasoning and summarisation. The optional medium 3B entry has no automatic mapping. Categories come from endpoint/mode metadata or an explicit general-chat task declaration, never LLM judgement or keyword heuristics. Resource preferences are expressed through the light default and explicit ordered mappings/fallbacks; there is no scheduling subsystem.

## 4. Fallback

Preferred -> explicitly ordered fallback list -> default -> clear failure. Candidates are deduplicated, capability/readiness checked and tried at most once. Missing, disabled, quarantined, incompatible or failed runtimes cannot be selected. Cached load failures require restart. Unknown override IDs fail clearly. Runtime generation/validation failures are not retried on another model. There is no arbitrary model selection, cloud fallback, implicit Ollama fallback or automatic downloading. Existing validators may expose generation unavailability as their fail-closed rejected-output result.

## 5. Lifecycle

No generation model loads at startup. One serialized owner closes the previous generation runtime before loading another and holds it through each full inference/stream. Native loading/inference cancellation drains before ownership release. Embeddings remain independently loaded and are excluded from generation selection. Optional Ollama explicitly unloads Grimoire's generation model before switching; an unload failure aborts the switch. Other daemon clients remain outside process memory ownership. No GPU/cluster scheduler was introduced.

## 6. CLI

Added registry-aware `models list`, `models inspect <id>`, `models policy`, `models test <id>`, evidence-backed `chat`, and `--model` overrides on chat, logs and evaluation. Chat supports `--mode general`, explicit `--task` and `--debug`. Existing commands/role-based model diagnostics remain. Inspection never loads weights; `models test` is explicit inference. JSON reports show configured models/policy or the actual selection/fallback. Status/doctor inspect the configured registry default.

## 7. UI and observability

A collapsed Model selector offers Auto and configured IDs, also applying to model-assisted logs/incidents. Status shows `Auto` plus the selected model display name, or the overridden model. Lazy configuration is distinguished from verified ready inference. Normal answer payloads omit routing details; `debug: true` adds request-scoped decisions. Internal structured logs record task, ID, rule, fallback attempts, provider and readiness without prompt/evidence text. Refusals requiring no generation record that explicitly.

## 8. Fixed 1.5B vs policy results

Fresh-process runs use native CPU, four requested threads, context 4096, evidence budget 8000 characters, the same MiniLM embedding settings and unchanged validators. Existing structured caps remain 512 factual / 384 log tokens. The stronger experiment explicitly maps only structured log classification to 3B to test whether that bounded task improves; it is not shipped as the default.

| Measure | Fixed 1.5B | Conservative Auto | Experimental logs -> 3B |
|---|---:|---:|---:|
| Development supported / oracle QA | 10/10 each | 10/10 each | 10/10 each |
| Development unsupported / oracle refusals | 4/4 each | 4/4 each | 4/4 each |
| Development logs / incidents | 6/6 each | 6/6 each | 6/6 each |
| Held-out complete evidence packets | 22/22 | 22/22 | 22/22 |
| Held-out supported / oracle QA | 22/22 each | 22/22 each | 22/22 each |
| Held-out unsupported / oracle refusals | 5/5 each | 5/5 each | 5/5 each |
| Held-out adversarial cases | 2/2 | 2/2 | 2/2 |
| Held-out logs / incidents | 7/7 each | 7/7 each | 7/7 each |
| Traceable / validated claims, development | 18/18 | 18/18 | 18/18 |
| Traceable / validated claims, held-out | 25/25 | 25/25 | 25/25 |
| Total workload seconds | 116.99 | 116.48 | 138.93 |
| Peak process RSS MiB | 2010.28 | 2017.24 | 3627.19 |

The earlier 3B comparison found equal supported aggregate QA but worse oracle QA, higher cost and different failures. This experiment does not overturn it: QA stays on 1.5B, and 3B is tested only for the explicit log capability. No claim is made about a 3B config/code reasoning advantage; a separately defined evaluation is required before mapping those tasks.

## 9. Latency

The 0.51-second fixed/Auto difference is run variation, not evidence of a speed improvement. Mean log request latency including switches: development 2.09 s fixed / 1.90 s Auto / 3.89 s experiment; held-out 2.34 / 2.26 / 4.42 s. Native small-model loads were 1.06-1.32 s in the full-suite runs. Experimental 3B loads were 1.95-2.58 s, plus closing the previous model (about 0.22-0.24 s). Total experiment cost was about 19% above conservative Auto.

## 10. Memory

Auto increased observed peak RSS by 6.96 MiB versus fixed in this single run; no statistical attribution is justified. The 3B experiment reached 3627.19 MiB versus Auto's 2017.24 MiB (about 80% higher). Real switch events returned process RSS to about 98 MiB after unloading before the next load. Peak RSS is the process high-water mark and does not drop after unloading; current RSS snapshots and event order establish lifecycle behavior. These are whole-process RSS measurements, including embeddings/evaluation where applicable, not GPU VRAM measurements.

## 11-12. Development and held-out reliability

All listed checks remain passing in all three measured modes. Every registered model receives the same evidence selection, factual extraction, slot/source/citation/scope/status validation, unsupported handling and log/incident validators. Actual routed HTTP tests verify malformed facts and log output are rejected for manual model selection in both streamed and non-streamed requests. General chat remains explicitly unvalidated prose. These previously exposed suites are regression material, not a new blind independent assessment.

## 13. Fallback tests

Automated checks cover missing, disabled, failed, incompatible, loading and incapable preferred models; failed loading; unready active runtimes; unknown overrides; no arbitrary fallback; duplicate/cyclic candidate deduplication through the bounded list; embedding exclusion; and clear errors when no allowed model is usable. A real missing-3B override selected 1.5B and recorded `model_missing` plus `fallback_used: true`. No cloud/Ollama service was used in the real runs. Optional Ollama unload failure is tested with a fake runtime; real daemon switching remains unmeasured.

## 14. Switching tests

The real probe returned `OK`, `OK.`, `OK` across small -> large -> small. Load times were 1.15 / 2.01 / 1.21 s; complete requests including switching took 1.41 / 2.80 / 1.84 s. Events alternate load/close with no overlapping generation ownership. Automated tests also cover serialization against streaming, cancellation while loading, switching back and aborting on unload failure. No stale provider references are passed into validation services.

## 15. Automated totals and evidence

373 backend tests and 5 frontend tests pass (378 total), including 41 new model-routing tests and the preserved provider-independence tests. Ruff lint/format and frontend syntax checks pass. The existing Starlette/httpx deprecation warning remains. Tests require normal Windows loopback access because asyncio socket-pair creation stalls inside this sandbox; no test needed model downloads or real weights.

Raw measurements: [fixed](fixed.json), [Auto](policy.json), [3B log experiment](log-experiment.json), [real switch/fallback](switch.json). The [machine summary](summary.json) is regenerated by `python scripts/audit_routing.py`; it checks protected first-run/suite hashes, equal scored summaries, complete evidence, actual selected IDs/tasks and real switching/fallback. `scripts/routing_value.py` reruns each mode explicitly with provisioned weights and refuses report overwrites. A final source manifest accompanies the reports; measurements are single-run observations, not a statistically controlled benchmark. OS cache, run order and concurrent lightweight tests were uncontrolled. No GPU or live Ollama benchmark was performed.

## 16. Does routing improve Grimoire?

It improves deliberate configuration, visibility, manual selection and safe lifecycle handling. **No meaningful answer-quality improvement is demonstrated.** The only stronger-model task experiment costs more while matching existing results. Keep the feature available and the default Auto policy effectively single-model. A configured capability is an operator assertion, not proof that a larger model is better.

## 17. Recommended next milestone

Operational deployment validation: Linux Compose, bounded-resource behavior, shutdown/restart, configured-index persistence and backup/restore rehearsal. Separately collect a new unseen task set, including an explicit configuration/code reasoning contract, before testing any stronger default policy. Do not add autonomous orchestration or integrations to justify routing complexity.
