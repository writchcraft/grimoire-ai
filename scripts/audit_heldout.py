"""Summarise preserved runs without changing fixtures, scores or application code."""

import hashlib
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "docs/heldout"
FIRST = DIRECTORY / "first-run.json"
assert (
    hashlib.sha256(FIRST.read_bytes()).hexdigest()
    == (DIRECTORY / "first-run.sha256").read_text().split()[0]
)
heldout = json.loads(FIRST.read_text())
development = json.loads((DIRECTORY / "development-before.json").read_text())

# Manual diagnoses from the untouched run, including oracle-only failures.
diagnoses = {
    "leaf-database": (
        [
            "evidence selection",
            "qualification spanning lines",
            "lexical relevance",
            "over-refusal",
        ],
        (
            "Compose evidence is ranked but dropped. Retrieved inventory supplies the right value "
            "from a different file, so the exact-evidence assertion fails. Oracle Compose refuses; "
            "nested DATABASE_NAME is not connected to its service in line candidates."
        ),
    ),
    "queue-link": (
        [
            "evidence selection",
            "qualification spanning lines",
            "expected-slot inference",
            "over-refusal",
        ],
        (
            "Ranked depends_on evidence is not supplied. Oracle also refuses; a YAML list child "
            "is not represented as the dependency relationship of its parent service. Retrieved "
            "answer emits unrelated inventory properties."
        ),
    ),
    "secondary-url": (
        ["overly broad alternatives", "scope reconciliation"],
        (
            "Retrieved answer passes; full-document oracle includes the primary repository too. "
            "The validator expands an accepted slot to every eligible candidate, beyond model selections."
        ),
    ),
    "retention-pair": (
        [
            "evidence selection",
            "expected-slot inference",
            "lexical relevance",
            "over-refusal",
        ],
        (
            "Two retention subjects in one question are not decomposed; both run modes refuse. "
            "All required text is top-5 but selected context is incomplete."
        ),
    ),
    "leaf-ports": (
        ["expected-slot inference", "qualification spanning lines"],
        (
            "Both required lines are supplied. The host-port slot does not cover the separately "
            "requested internal port; both runs omit 8126."
        ),
    ),
    "exporter-list": (
        ["lexical relevance", "qualification spanning lines", "over-refusal"],
        (
            "Both lines are supplied but the list-level Telegraf qualifier is disconnected from "
            "host/port lines under an Exporter list heading. Both modes refuse."
        ),
    ),
    "database-groups": (
        [
            "scope reconciliation",
            "qualification spanning lines",
            "lexical relevance",
            "overly broad alternatives",
        ],
        (
            "Production and staging YAML groups share chunk metadata. Both ports are rendered; "
            "allowed_subnet is not matched as the subnet attribute. Both modes fail."
        ),
    ),
    "replication-off": (
        [
            "evidence selection",
            "negative/unchanged fact handling",
            "qualification spanning lines",
            "over-refusal",
        ],
        (
            "Nested replication_enabled is not tied to its production service scope. Required "
            "evidence is ranked but dropped; full oracle also refuses."
        ),
    ),
    "spare-radio": (
        [
            "overly broad alternatives",
            "scope reconciliation",
            "qualification spanning lines",
        ],
        (
            "Retrieved answer passes. Full oracle adds the production port to a spare/rehearsal "
            "question; adjacent prose qualification does not constrain eligible alternatives."
        ),
    ),
    "quoted-wrong-port": (
        [
            "evidence selection",
            "lexical relevance",
            "conflict handling",
            "overly broad alternatives",
            "over-refusal",
        ],
        (
            "Retrieved answer quotes a disproved 6543 guess as an answer alternative alongside "
            "its correction. Expected inventory line is ranked but dropped. Oracle refuses even "
            "with that line available. Provenance validation accepts the faithfully quoted wrong guess."
        ),
    ),
    "absent-firmware": (
        ["lexical relevance", "unsupported claim", "validation"],
        (
            "Both runs mark the answer grounded by selecting a sentence prohibiting inference "
            "of firmware version. No version is invented, but an explicit refusal is required. "
            "Lexical attribute matching mistakes a limitation for positive evidence."
        ),
    ),
}


def metrics(report):
    supported = [r for r in report["qa"] if r["retrieval"]["expected_evidence"]]
    latency = {}
    for name, samples in {
        "supported_answer_seconds": [r["timing"].get("seconds", 0) for r in supported],
        "oracle_answer_seconds": [
            r["oracle_timing"].get("seconds", 0) for r in supported
        ],
        "retrieval_prepare_including_embedding_seconds": [
            r["retrieval"]["debug"]["prepare_seconds_including_embedding"]
            for r in supported
        ],
        "hybrid_search_seconds": [
            r["retrieval"]["debug"]["seconds"] for r in supported
        ],
        "supported_end_to_end_seconds": [
            r["retrieval"]["debug"]["prepare_seconds_including_embedding"]
            + r["timing"].get("seconds", 0)
            for r in supported
        ],
        "log_answer_seconds": [r["timing"]["seconds"] for r in report["logs"]],
    }.items():
        samples.sort()
        latency[name] = {
            "median": statistics.median(samples),
            "max": max(samples),
            "p95_nearest_rank": samples[
                min(len(samples) - 1, int(len(samples) * 0.95))
            ],
        }
    return {
        "summary": report["summary"],
        "complete_supplied_context": sum(
            r["retrieval"]["context_complete"] for r in supported
        ),
        "supported_total": len(supported),
        "missing_required_markers": sum(
            len(r["generation"]["missing_facts"]) for r in supported
        ),
        "latency": latency,
    }


failures = []
for row in heldout["qa"]:
    if (
        row["generation"]["status"] == "PASS"
        and row["oracle_generation"]["status"] == "PASS"
    ):
        continue
    categories, reason = diagnoses[row["id"]]
    failures.append(
        {
            "id": row["id"],
            "categories": categories,
            "diagnosis": reason,
            "retrieved": row["generation"],
            "oracle": row["oracle_generation"],
            "context_complete": row["retrieval"]["context_complete"],
        }
    )
assert {f["id"] for f in failures} == set(diagnoses)
freeze = json.loads((DIRECTORY / "freeze.json").read_text())
runtime_changes = [
    p
    for p, digest in freeze["sha256"].items()
    if p.startswith(
        (
            "backend/app/services/",
            "backend/app/rag/",
            "backend/app/ai/",
            "backend/app/ingestion/",
        )
    )
    and hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != digest
]
assert not runtime_changes
result = {
    "first_run_sha256": hashlib.sha256(FIRST.read_bytes()).hexdigest(),
    "heldout_first_and_final": metrics(heldout),
    "development_regression": metrics(development),
    "failure_classification": failures,
    "runtime_changes_after_freeze": runtime_changes,
    "general_fixes": [],
    "decision": "NO: complete ranked evidence does not reliably survive selection; scope, "
    "attribute inference and relevance defects dominate. No routing introduced.",
    "final_run_policy": "No reliability changes or repeat held-out inference. First run is "
    "also the final measured result, not an after-fixes result.",
}
(DIRECTORY / "analysis.json").write_text(
    json.dumps(result, indent=2) + "\n", encoding="utf-8"
)
print(
    json.dumps(
        {k: v for k, v in result.items() if k != "failure_classification"}, indent=2
    )
)
