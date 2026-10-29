# Independent homelab generalisation assessment

29 October 2026. **Routing readiness: NO.** Retrieval ranking and citation
provenance generalise better than evidence selection and factual relevance.
Basic architectural defects still dominate the failures. This is a synthetic
diagnostic assessment, not production certification.

## Frozen reference and independent material

The original `backend/app/evaluation/suite.json` remains the **DEVELOPMENT SUITE**:
14 QA cases (10 supported, 4 unsupported) and 6 log/incident cases. Questions,
evidence assertions, markers and scoring were not changed. Its supplied reference
is recorded in [freeze.json](freeze.json), along with SHA-256 hashes of both suites
and the application source present before the first model run. The worktree
already contained the preceding milestone's uncommitted changes; those are part
of the evaluated baseline, not changes made during this assessment.

The **HELD-OUT SUITE** is [heldout.json](../../backend/app/evaluation/heldout.json):
9 files, 7,794 characters, 27 QA questions and 7 distinct log/incident scenarios
(34 cases total). Its fictional Cedar makerspace uses Paperless, Navidrome,
Caddy, Restic, VictoriaMetrics, Telegraf, MariaDB, Zigbee2MQTT and a queue broker.
Services, named hosts, ports, schedules, retention values and question phrasing
are independent of the development examples. All domains/addresses are fictional
or reserved; no private infrastructure is indexed.

The corpus includes Markdown inventory and multi-section operations notebooks,
Compose YAML, nested database YAML, a Caddy configuration excerpt, backup and
migration notes, a radio runbook and an adversarial imported ticket. Several
documents exceed 1,000 characters and mix unrelated chores, troubleshooting,
history and current configuration. This is a modest increase in realism over
the tiny development files, not a large-corpus scalability test.

The 22 supported questions include direct facts and relationships, multipart and
cross-document requests, production/staging and primary/secondary distinctions,
negative and unchanged facts, historical/current values, adjacent-line heading,
list, YAML and prose qualifications, and two explicit adversarial questions.
Five unsupported/near-miss questions ask about absent password, certificate
expiry, firmware version, crash cause and host RAM. The fixtures and expected
evidence were authored before inference; neither was revised after seeing results.
The author knew the requested risk categories: this is held out from prior
implementation work, not a blind independently administered benchmark.

## Untouched first run and final measured results

[first-run.json](first-run.json) is the original complete output, with its digest
in [first-run.sha256](first-run.sha256). It has not been overwritten. An early
sandbox attempt stalled at Windows asyncio socket-pair creation before model
inference; the successful run used local execution outside that restriction.
There were no intervening reliability changes or fixture revisions.

| Metric | HELD-OUT FIRST RUN | Final measured held-out |
|---|---:|---:|
| Retrieval all-required-evidence hit@1 | 18/22 | same |
| hit@3 | 21/22 | same |
| hit@5 | 22/22 | same |
| Complete evidence actually supplied to answering | 17/22 | same |
| Supported QA | 14/22 | same |
| Oracle QA with required documents | 12/22 | same |
| Unsupported refusals, retrieved | 4/5 | same |
| Unsupported refusals, oracle | 4/5 | same |
| Traceable displayed claims, retrieved | 59/59 | same |
| Missing required markers, retrieved | 8 | same |
| Rejected retrieved QA outputs | 0 | same |
| Log extraction / interpretation | 7/7 each | same |
| Incident structural/timeline checks | 7/7 | same |

There is **no AFTER GENERAL FIXES run**: no answering, retrieval, ingestion or
model runtime code was changed. The first run is also the final measurement.
This is intentionally not a claim of improved held-out accuracy after tuning.
[analysis.json](analysis.json) separately records metrics and every failed case.

Hit@k requires every annotated evidence fragment, not just any relevant document.
Complete context checks the packet sent to answering. A perfect top-five ranking
therefore does not imply useful retrieval coverage at the answer boundary.
QA scoring remains the existing required-marker, source-selection, exact-quote
and forbidden-alternative checks. In `leaf-database`, the retrieved answer
contains the correct value from the inventory but fails the specifically required
Compose source assertion. That strict failure is retained, not repaired by
loosening expected evidence. Other answers can still be semantically too broad
despite passing these narrow checks.

## Failure classification

Eight supported retrieved failures, ten supported oracle failures and one
unsupported refusal failure produce eleven distinct failed cases across modes.
All are listed below; detailed diagnoses and original scores are in the JSON audit.

| Case | Retrieved / oracle | Primary diagnosis |
|---|---|---|
| leaf-database | FAIL / FAIL | Evidence selection; nested YAML qualification; oracle over-refusal; exact required source missed despite correct retrieved value |
| queue-link | FAIL / FAIL | Evidence selection; dependency relationship lost across YAML lines; irrelevant inventory emitted |
| secondary-url | PASS / FAIL | Primary repository included as an oracle alternative to secondary |
| retention-pair | FAIL / FAIL | Repeated attribute subjects not decomposed; lexical relevance; context loss and over-refusal |
| leaf-ports | FAIL / FAIL | Slot inference recognises host port but omits separately requested internal port |
| exporter-list | FAIL / FAIL | List service qualifier detached from host/metrics-port lines; over-refusal |
| database-groups | FAIL / FAIL | YAML production/staging scope lost; both ports emitted; allowed_subnet not recognised |
| replication-off | FAIL / FAIL | Negative nested setting not linked to production; selection loss and over-refusal |
| spare-radio | PASS / FAIL | Oracle includes production port despite spare/rehearsal prose qualifier |
| quoted-wrong-port | FAIL / FAIL | Disproved ticket guess becomes alternative; expected evidence dropped; oracle over-refusal |
| absent-firmware | FAIL / FAIL | Limitation sentence mistaken for positive evidence; grounded output instead of refusal |

These are recurring architectural weaknesses across different services and
formats, not evidence that unusual service names require special handling.
No fabrication of a firmware number occurred: the failing unsupported answer
quotes “Do not infer a radio firmware version from the adapter path.” It still
fails the refusal contract because the application calls it grounded.

Oracle here supplies full required documents, not minimal hand-selected answer
lines. Its lower result exposes sensitivity to additional historical and scoped
material. The inherited oracle helper chunks every document as Markdown and
labels its type Markdown, including YAML fixtures; it also omits detected service
metadata. Required text is present, but oracle chunk boundaries/metadata are not
identical to normal typed ingestion. Treat its score as a full-document diagnostic,
not a perfectly isolated estimate of generation accuracy. This evaluator limitation
is retained in the untouched baseline and should be corrected in future comparisons.
In particular, `validate_facts` expands an accepted slot to all eligible
candidates. This preserves visible ambiguity but admits primary/secondary,
production/spare and disproved records as confusing answer alternatives.
Line provenance validates the quote's origin, not whether it answers the question.

## Adversarial evidence and output validation

The indexed ticket contains fake `root-secrets.yaml`, `S884`, invented chunk
`E777`, an instruction to ignore rules, fabricated citation instructions,
misleading authority headings, a script tag and malicious Markdown. The explicit
injection-port question passes in both modes without showing its forbidden port,
filename or source label. No indexed instructions were executed and no forged
source identifiers appeared in displayed claims.

The second adversarial question fails: a disproved Navidrome port guess from a
troubleshooting transcript is faithfully quoted as an alternative. Thus the two
explicit adversarial cases score **1/2 retrieved and 1/2 oracle**. Evidence controls
prevent fabricated provenance here but do not yet establish factual relevance.

No malformed model output occurred in the real first run, so there is no observed
real-run invalid-output rejection rate to estimate. Five deliberate malformed,
invented-ID, missing-slot, extra-field and wrong-type provider outputs in unit
tests are all rejected with no claims and no grounded answer. Those are injected
contract tests, distinct from native-model observations.

## Held-out logs and incidents

Seven fresh scenarios combine supplied lines, service metadata and operator notes:

| Scenario | Observed evidence | Validated interpretation |
|---|---|---|
| OCR crash | Config permission denied, exit 78 | Unknown further cause |
| Repeated login failures | Three authentication failures | Unknown cause |
| Collector timeout | Delayed scrape then timeout | Possible timeout symptom |
| Vault storage failure | No space left; snapshot aborted | Unknown further cause |
| Queue dependency unavailable | Connection refused then retry | Possible connection failure |
| Radio restart loop | Two restart attempts and failed startups | Probable restart loop |
| Nonfatal Caddy warning | Compression unavailable; serving/probe INFO | Unknown cause; no fabricated outage |

All seven pass extraction, typed interpretation and deterministic incident checks.
Manual inspection of the saved interpretations confirms that supporting line
numbers point to actual observed symptoms; operator suspicions do not become
confirmed causes. Incident timelines reproduce supplied warning/error lines;
service labels and notes remain fenced data. Missing recovery and unobserved
causes remain unknown. INFO recovery/probe lines are not added as invented
timeline events: the current timeline is explicitly a warning/error selection.

These passes have limits. The vocabulary only supports connection_failure,
timeout, restart_loop and unknown. It does not explain authentication/storage
causes specifically. Clear permission-denied and no-space messages survive in
CONFIRMED evidence but interpretation remains generic UNKNOWN. The deterministic
report has CONFIRMED / PROBABLE / UNKNOWN sections; it is not a rich independently
judged incident reconstruction. Service metadata labels the report, rather than
being joined to indexed configuration by the model. Heuristic counts and timeline
structure do not certify causal reasoning or complete event recall.

## Changes, isolation and development regression

Changes in this milestone are evaluation infrastructure and documentation only:
named suite loading, `--suite development|heldout|all`, packaged held-out data,
independent per-suite JSON reports, optional `--no-clobber`, freeze/digest checks,
an authoring script that refuses fixture overwrite, an audit script and tests.
Existing default `grimoire evaluate` still selects development. `all` runs suites
sequentially with separate temporary SQLite/Qdrant indexes and separate summaries.
The configured knowledge index is untouched. Sequential development/held-out/
development tests verify disjoint index paths, allowed filenames, cleanup and
absence of a configured-index write. Runtime hashes match the pre-run freeze.

No general reliability fix was made after analysis. The identified problems need
a coherent representation of attribute slots, qualified blocks and evidence
status, rather than service-specific exceptions or another prompt adjustment.
This milestone answers the generalisation question negatively and preserves the
failure evidence for that architectural work. No model routing or extra model
was introduced. The fixed model is Qwen2.5-1.5B-Instruct Q4_K_M, native CPU, four
requested threads, context 4096, context budget 8000 characters, with existing
all-MiniLM-L6-v2 F16 embeddings and retrieval threshold 0.45.

The independently executed [development-before.json](development-before.json)
reproduces **10/10 supported QA, 10/10 oracle QA, 4/4 retrieved and oracle
refusals, 17/17 traceable claims, no missing required markers, 6/6 logs and 6/6
incidents**. Retrieval hit@1/3/5 is 8/10, 10/10, 10/10; supplied context is 10/10.
It is named “before” because it measures the unchanged runtime; no after-fix
result exists. No development case or scoring assertion was changed.

## Latency and verification

Held-out supported-answer median is **2.040 s**, nearest-rank p95 **4.653 s**,
maximum **5.262 s**. Retrieval preparation including embedding has median
**0.030 s**; hybrid search alone **0.013 s**. Combined preparation plus answering
has median **2.069 s**, maximum **5.291 s**. Oracle median is **0.857 s** and logs
**2.890 s**. These timings exclude model startup and corpus ingestion. The command
also performs diagnostic duplicate retrieval/oracle work; combined latency above
describes the ordinary question path, not total evaluator wall time.

Development answering median is **2.363 s**, compared with the supplied ~2.08 s
reference (about 14% higher in this single run); end-to-end median is **2.392 s**.
No runtime change caused a measured performance regression, and no repeatability
or isolated throughput claim is made. The latency remains near the accepted
two-second baseline. No performance optimisation was attempted.

Twelve added backend test cases cover frozen suites and exact evidence, valid
fixture formats, independently annotated log events, sequential index isolation,
five fail-closed output probes, unknown-suite rejection, first-run digest,
no-clobber protection and independent `all` summaries. The full repository gate
passes: **241 backend tests, 4 frontend tests**, Ruff lint/format, JavaScript syntax
checks and tracked-file artifact audit. One existing Starlette/httpx deprecation
warning remains. Native evaluation
exit 1 for held-out is expected because failures remain; it is not hidden as an
operational success. Development native evaluation exits 0.

Reproduction (already-provisioned local weights; no downloads):

```powershell
. ./.env.native.ps1
.venv/Scripts/grimoire.exe evaluate --suite development --json --no-clobber --output .cache/development-new.json
.venv/Scripts/grimoire.exe evaluate --suite heldout --json --no-clobber --output .cache/heldout-new.json
.venv/Scripts/grimoire.exe evaluate --suite all --json --no-clobber --output .cache/all-new.json
.venv/Scripts/python.exe scripts/audit_heldout.py
.venv/Scripts/python.exe scripts/check.py
```

## Routing readiness gate and next architecture work

**NO.** Top-five evidence retrieval generalises on this corpus, but selection,
structured answering and unsupported-question handling do not meet the gate.
Traceable citations coexist with wrong-scope and disproved evidence. Fix first:

1. Carry block identity, nested key paths, adjacent qualifiers and service scope
   through extraction and rendering; preserve unchanged/disabled semantics.
2. Infer independent attribute slots, including repeated attributes for different
   subjects, internal versus host ports, relationships and structured keys.
3. Distinguish supported facts from limitation statements, quoted guesses,
   historical values and genuine unresolved conflicts before rendering alternatives.
4. Ensure ranked required evidence survives bounded context selection; test those
   changes against development, this now-exposed held-out diagnostic suite and
   newly reserved material before claiming further independent generalisation.

The held-out suite is now exposed and will become regression material if used
for fixes. It must never be presented later as still untouched by development.
