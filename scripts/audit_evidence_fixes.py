"""Verify immutable references and summarize the separately saved after-fix run.

Never writes the first-run report, digest, original analysis or suite fixtures.
"""

import hashlib
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "docs/heldout"


def read(name):
    return json.loads((DIRECTORY / name).read_text(encoding="utf-8"))


def save_once(name, value):
    path = DIRECTORY / name
    if path.exists():
        assert read(name) == value, f"Refusing to overwrite a different report: {name}"
    else:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")


def metrics(report):
    supported = [r for r in report["qa"] if r["retrieval"]["expected_evidence"]]
    latency = {}
    for key, samples in {
        "supported_answer_seconds": [r["timing"].get("seconds", 0) for r in supported],
        "supported_end_to_end_seconds": [
            r["timing"].get("seconds", 0)
            + r["retrieval"]["debug"]["prepare_seconds_including_embedding"]
            for r in supported
        ],
        "prepare_seconds": [
            r["retrieval"]["debug"]["prepare_seconds_including_embedding"]
            for r in supported
        ],
        "oracle_seconds": [r["oracle_timing"]["seconds"] for r in supported],
    }.items():
        samples.sort()
        latency[key] = {
            "median": statistics.median(samples),
            "max": max(samples),
            "p95_nearest_rank": samples[
                min(len(samples) - 1, int(len(samples) * 0.95))
            ],
        }
    return {
        "summary": report["summary"],
        "complete_evidence_packets": sum(
            r["retrieval"]["context_complete"] for r in supported
        ),
        "supported_total": len(supported),
        "adversarial": {
            mode: {
                r["id"]: r[mode]["status"]
                for r in report["qa"]
                if r["category"] == "adversarial"
            }
            for mode in ("generation", "oracle_generation")
        },
        "oracle_citations": {
            "traceable": sum(
                any(
                    c["source"] == s["label"] and c["quote"] in s["text"]
                    for s in r["oracle_answer"]["sources"]
                )
                for r in report["qa"]
                for c in r["oracle_answer"]["claims"]
            ),
            "total": sum(len(r["oracle_answer"]["claims"]) for r in report["qa"]),
        },
        "latency": latency,
    }


def main():
    for path, expected in read("protected-baseline.json").items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    for path, expected in read("verified-evidence-freeze.json")["sha256"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    scorer = "backend/app/evaluation/runner.py"
    assert (
        hashlib.sha256((ROOT / scorer).read_bytes()).hexdigest()
        == read("after-general-fixes-freeze.json")["sha256"][scorer]
    )
    reports = read("verified-evidence-all.json")["suites"]
    assert [r["suite"] for r in reports] == ["development", "heldout"]
    development, heldout = reports
    save_once("after-general-fixes.json", heldout)
    save_once("development-after-general-fixes.json", development)
    first = read("first-run.json")
    for report, baseline in (
        (heldout, first),
        (development, read("development-before.json")),
    ):
        assert report["suite_sha256"] == baseline["suite_sha256"]
        assert report["generation_model"] == baseline["generation_model"]
        assert report["embedding_model"] == baseline["embedding_model"]
        assert report["configuration"] == baseline["configuration"]
    result = {
        "FIRST RUN": metrics(first),
        "AFTER GENERAL FIXES": metrics(heldout),
        "DEVELOPMENT BEFORE": metrics(read("development-before.json")),
        "DEVELOPMENT AFTER GENERAL FIXES": metrics(development),
        "remaining_failed_cases": [
            {
                "id": r["id"],
                "category": r["category"],
                "retrieved": r["generation"],
                "oracle": r["oracle_generation"],
                "complete_packet": r["retrieval"]["context_complete"],
            }
            for r in heldout["qa"]
            if r["generation"]["status"] != "PASS"
            or r["oracle_generation"]["status"] != "PASS"
        ],
        "protected_baseline_verified": True,
        "runtime_freeze_verified": True,
        "INTERMEDIATE RUNS": {
            name: {report["suite"]: metrics(report) for report in read(name)["suites"]}
            for name in (
                "after-general-fixes-all.json",
                "final-general-fixes-all.json",
                "qualified-pipeline-v2-all.json",
                "final-evidence-all.json",
                "semantic-final-all.json",
            )
        },
        "iteration_policy": "All completed combined runs retained with source freezes. "
        "The first follow-up corrected quote bounds and flat qualifier lineage before held-out "
        "outcome review. Subsequent fixes address general planner, multiword key, anaphora, "
        "cross-reference and typed-value preservation defects, with model-independent tests. "
        "Manual review of the all-pass penultimate run then corrected equivalent-value "
        "conflict labels, hostname versus host-port extraction and elliptical scope inheritance. "
        "Two early inference attempts were cancelled after unit-test failures; they produced "
        "no complete result. No best-of-run selection or fixture/scorer changes.",
        "evaluation_policy": "Identical suites, expectations, scorer, oracle helper, model and "
        "runtime settings. Previously exposed held-out suite is now regression "
        "material, not a new blind assessment.",
    }
    save_once("analysis-after-general-fixes.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
