# Evidence preservation and qualification milestone

29 October 2026. **Routing readiness: YES for the next model registry / policy-selection milestone on this diagnostic evidence.** No registry, routing policy, additional model or 3B reintroduction was implemented. The fixed generator remains **Qwen2.5-1.5B-Instruct Q4_K_M**, native CPU, four requested threads, context 4096, context budget 8000 characters, with the existing MiniLM embeddings and retrieval threshold 0.45.

The final measured version is [verified-evidence-all.json](verified-evidence-all.json), with its [application source freeze](verified-evidence-freeze.json). Separate final reports are [held-out AFTER GENERAL FIXES](after-general-fixes.json) and [development AFTER GENERAL FIXES](development-after-general-fixes.json). The [machine-readable comparison](analysis-after-general-fixes.json) includes every completed intermediate combined run and all latency statistics.

## Immutable FIRST RUN

[first-run.json](first-run.json), [first-run.sha256](first-run.sha256) and the original [failure classifications](analysis.json) are unchanged. [protected-baseline.json](protected-baseline.json) records their original hashes and the hashes of both suites. The original [assessment report](report.md) remains unchanged too. Neither expected outcomes nor scoring assertions were edited. The evaluation runner/oracle helper was not changed during this milestone; its hash matches the earliest general-fix source freeze. The older original freeze predates named-suite evaluation infrastructure, so it is not used to assert the evaluator's milestone hash.

The comparison audit checks protected hashes, final runtime hashes, identical suite digests, identical generation/embedding identities and identical runtime settings. The previously exposed held-out suite is now regression material; these results are **not another blind independent assessment**.

## Exact loss mechanisms and fixes

1. **After ranking:** evidence selection retained only one winner per recognized fact, applied an additional score threshold, searched section siblings from only the first four ranked hits, and capped expansion at eight chunks. A broad high-scoring clause could win repeatedly while a necessary lower-ranked source disappeared. Selection now considers qualified evidence across the ranked pool and same-section siblings, gives each independent answer slot coverage priority, then retains distinct matching source records. Ranking coefficients and its original question planner remain unchanged.
2. **Deduplication:** text-only equality could collapse identical values under different parent scopes or from distinct required sources. Ranking dedup now checks heading, lineage and service context; context assembly also retains document identity and exact span. A general regression demonstrates the real defect with equal continuation text under production and staging headings. This is a dedup correctness fix, not hybrid-score retuning.
3. **Budget enforcement:** whole chunks were admitted greedily in incoming score order, with no knowledge of remaining slots. Context assembly now compacts to complete qualified source blocks when necessary, prioritizes uncovered slots, and marks a slot covered only after its block fits. It does not clip values away from qualifiers. When everything fits, ranking remains the tie-breaker.
4. **Structured extraction:** line-only candidates lost sibling `environment`, service/list identity, multiword mapping keys and parent headings. Extraction now consumes qualified records and metadata instead. The old 1200-character extraction/quote bound could also silently discard a valid 1201–2400-character ingestion block; extraction and validation now share the 2400-character block bound.
5. **Slot inference:** repeated attributes with different subjects, host names versus host ports, internal/container versus published ports, dependency relationships, incidental location nouns and elliptical follow-ups were conflated. The separate answer planner recognizes independent requested fields, supports arbitrary config-key phrases, uses the requested attribute region, and preserves inherited entity/scope only for real anaphora/ellipsis. Numeric context is not itself another slot. None of these rules names a held-out service or case.
6. **After model selection:** accepting one candidate expanded the answer to every lexical candidate, including unrelated, wrong-scope and disproved text. Validation now honors explicit eligible IDs. A conservative application-owned guard can recover an empty selection from an explicit qualified port, subnet or retention value; it cannot recover a guess, limitation or policy pointer. Recovery is recorded as `typed_value_guard`, and original source text and polarity remain unchanged. Malformed output still fails closed.

## Qualification, scope, status and relationships

YAML/list records retain their peer properties and nested key path, including multiword labels such as `External access`. New list objects and peer mappings remain separate. Heading hierarchy, mapping ancestry, service/environment/role labels and original line references survive long-block chunk boundaries in separate `lineage` metadata. Qualifiers are not invented quote lines. Each fact retains original value/quote, source, start/end line, scope labels, qualifier text, semantic status and polarity. SQLite adds lineage without rewriting vectors or source text. **Previously indexed large structured documents need reingestion to populate new lineage metadata**; the configured knowledge index was not rewritten by evaluation.

Scope screening distinguishes production/staging/development, internal/external, primary/secondary/explicit backup roles, source/destination, old/new, spare/main and enabled/disabled profiles. Backup activity is not automatically a backup-instance role. A polarity question can discover a disabled setting rather than filtering it out. Rehearsal qualification remains bound to its setting. Explicit record identity takes precedence over a broad document service tag. Host-port evidence cannot fill a hostname slot.

The small status vocabulary is **asserted, negated, historical, superseded, unknown, disproved, hypothetical**. Negated polarity is retained separately when a historical/superseded parent applies. Historical/old sections do not supply current values; previous-only requests exclude current values. Explicit disproval, guesses and hypothetical statements cannot become affirmative candidates.

Relationship labels compare recognized values and polarity within explicit scopes. Equivalent URLs, ports and retention quantities are not conflicts merely because prose or sources differ. Explicitly different scopes yield alternatives, same-scope current contradictions yield conflicts, and differing historical/current values can be classified as superseded. Missing scope alone does not create an alternative. “Does not use port A” can coexist with “uses port B”; opposite polarity on the same port is a conflict. Uncomparable prose remains quoted evidence without an invented conflict label.

Explicit unknown/limitation statements and cross-references are availability evidence, not values. An unknown-only packet returns insufficient evidence without a model call. Firmware-version questions no longer acquire an unrelated adapter-path slot. The final refusal says the available documentation does not specify the value, with no affirmative value claim, instead of grounding an answer in an adapter-path or limitation sentence.

## FIRST RUN vs AFTER GENERAL FIXES

| Metric | FIRST RUN | AFTER GENERAL FIXES |
|---|---:|---:|
| Retrieval all-required-evidence hit@1 | 18/22 | 18/22 |
| hit@3 | 21/22 | 21/22 |
| hit@5 | 22/22 | 22/22 |
| Complete evidence packets | 17/22 (77.3%) | 22/22 (100%) |
| Supported QA | 14/22 (63.6%) | 22/22 (100%) |
| Oracle QA | 12/22 (54.5%) | 22/22 (100%) |
| Unsupported refusals, retrieved | 4/5 | 5/5 |
| Unsupported refusals, oracle | 4/5 | 5/5 |
| Explicit adversarial cases, retrieved | 1/2 | 2/2 |
| Explicit adversarial cases, oracle | 1/2 | 2/2 |
| Traceable displayed claims, retrieved | 59/59 | 25/25 |
| Traceable displayed claims, oracle | 49/49 | 24/24 |
| Logs: extraction / interpretation | 7/7 each | 7/7 each |
| Incidents | 7/7 | 7/7 |
| Rejected retrieved outputs | 0 | 0 |

The citation denominator decreased because one complete qualified block can cover several fields, and unselected lexical records are no longer appended. This is not a claim of fewer atomic facts being required. Both explicit adversarial questions now pass in both modes: embedded instructions/forged provenance remain excluded, and a disproved guess is not emitted as a current alternative. Manual review also checked displayed scope, unchanged/negative statements and false-conflict labels, beyond the narrow scorer.

## Frozen DEVELOPMENT regression

Supported and oracle QA remain **10/10 each**; retrieved and oracle unsupported refusals remain **4/4 each**. Complete packets remain 10/10. Retrieval hit@1/3/5 remains 8/10, 10/10, 10/10. Displayed citations are **18/18 retrieved and 15/15 oracle**, all traceable. Log extraction/interpretation and incidents remain **6/6 each**. No final development quality regression is hidden. Earlier temporary regressions are retained below.

## Latency

| Measurement | FIRST RUN | AFTER GENERAL FIXES |
|---|---:|---:|
| Supported answer median | 2.040 s | 1.257 s |
| Supported answer p95, nearest rank | 4.653 s | 3.080 s |
| Supported answer maximum | 5.262 s | 4.586 s |
| Preparation median, including embedding | 0.030 s | 0.042 s |
| Preparation + answering median | 2.069 s | 1.299 s |
| Oracle answer median | 0.857 s | 0.724 s |

Qualification/coverage preparation adds about 11.7 ms at the median. Supported answering remains practical and is faster in this final run; there was no correctness-for-latency trade-off. Development answering median is 2.240 s versus 2.363 s in the preserved development-before run. These are sequential CPU runs, not an isolated statistical speedup experiment. Startup and ingestion are excluded. The original latency convention is retained: the two first-run supported questions with no answering call contribute zero recorded answer-stage time. Intermediate native timings varied; the final run, not the fastest run, supplies this comparison.

## Tests, iteration history and limits

**91 new model-independent test cases** in [test_qualified_evidence.py](../../backend/tests/test_qualified_evidence.py) cover independent/repeated/arbitrary slots, low-ranked evidence, budget coverage, qualified YAML/lists and multiword keys, inherited lineage and persistence, scope pairs, primary/backup roles, old/current/previous-only semantics, unchanged and explicit negative values, superseded/disproved/hypothetical text, unknown statements, policy pointers, safe typed-value recovery, equivalent values, true conflicts and non-conflicting constraints. Existing selection-contract tests were adapted to explicit ID selection and quote-only model input; neither evaluation fixture nor scorer changed.

The complete repository gate passes **332 backend tests, four frontend tests**, Ruff lint/format, frontend syntax, diff whitespace and tracked-file artifact checks. One pre-existing Starlette/httpx deprecation warning remains. Windows sandbox asyncio initialization stalled before inference, so native evaluations and full async tests ran through the approved local execution path. Final native evaluation exits 0.

Every completed combined intermediate run is retained with its source freeze; none overwrites FIRST RUN:

| Intermediate artifact | Complete packets | Held-out QA | Oracle QA | Retrieved refusals | Dev QA | Dev oracle |
|---|---:|---:|---:|---:|---:|---:|
| [after-general-fixes-all.json](after-general-fixes-all.json) | 18/22 | 17/22 | 15/22 | 4/5 | 9/10 | 8/10 |
| [final-general-fixes-all.json](final-general-fixes-all.json) | 18/22 | 17/22 | 15/22 | 4/5 | 9/10 | 8/10 |
| [qualified-pipeline-v2-all.json](qualified-pipeline-v2-all.json) | 22/22 | 21/22 | 22/22 | 5/5 | 9/10 | 10/10 |
| [final-evidence-all.json](final-evidence-all.json) | 22/22 | 22/22 | 22/22 | 5/5 | 10/10 | 10/10 |
| [semantic-final-all.json](semantic-final-all.json) | 22/22 | 22/22 | 22/22 | 5/5 | 10/10 | 10/10 |

The first follow-up addressed quote limits/flat lineage before held-out outcome review. Later iterations addressed general planner, qualified-record, cross-reference and explicit-value defects. Manual inspection of an all-pass intermediate result caught false conflict labels and host-port leakage that the scorer did not penalize; those received generic regressions and a subsequent native rerun. Two early inference attempts were cancelled after general unit-test failures and produced no complete reports. Development-only probes remain in `.cache`. The final source freeze and final run are used irrespective of earlier faster or all-pass results.

**Remaining observed failed cases: none in the final frozen suites.** Remaining failure risks include complex cross-paragraph coreference, unmarked chronology, mixed current/disproved clauses in one sentence, unfamiliar structured syntax and large packets beyond fixed budgets. Status/scope screening is deterministic and deliberately bounded, not a proof of arbitrary semantic entailment. The unchanged oracle helper still treats supplied documents as Markdown, including YAML; this is retained for comparison. The small exposed synthetic suites do not establish large-corpus or production accuracy.

## Routing-readiness verdict

**YES for the next model registry / policy-selection milestone.** This verdict is based on the post-fix held-out evidence: 22/22 complete packets, 22/22 supported and oracle QA, 5/5 safe unsupported refusals in both modes, 2/2 adversarial passes in both modes, retained scope/qualification semantics, and no remaining measured basic evidence-pipeline failure. Perfect development results are a regression check, not the justification. Further independent material can test the remaining heuristic limits; no routing or additional model was added here.

Reproduce on the provisioned local runtime with a new output path:

```powershell
. ./.env.native.ps1
.venv/Scripts/grimoire.exe evaluate --suite all --json --no-clobber --output .cache/new-evidence-evaluation.json
.venv/Scripts/python.exe scripts/audit_evidence_fixes.py
.venv/Scripts/python.exe scripts/check.py
```

The audit verifies the saved final run and refuses to overwrite differing reports; a new evaluation is intentionally written separately.
