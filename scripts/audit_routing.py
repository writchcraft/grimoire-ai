"""Audit saved routing measurements without modifying frozen evaluation outcomes."""

import hashlib
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    folder = ROOT / "docs/routing"
    protected = json.loads((ROOT / "docs/heldout/protected-baseline.json").read_text())
    assert all(digest(ROOT / path) == expected for path, expected in protected.items())
    reports = {
        name: json.loads((folder / f"{name}.json").read_text())
        for name in ("fixed", "policy", "log-experiment", "switch")
    }
    summary = {"protected_baseline_hashes_match": True, "runs": {}}
    fixed = reports["fixed"]
    for name, report in reports.items():
        results = []
        for index, suite in enumerate(report["suites"]):
            baseline = fixed["suites"][index]
            assert suite["suite_sha256"] == baseline["suite_sha256"]
            assert suite["summary"] == baseline["summary"]
            supported = [
                row for row in suite["qa"] if row["retrieval"]["expected_evidence"]
            ]
            assert all(row["retrieval"]["context_complete"] for row in supported)
            for row in suite["qa"]:
                if name != "fixed":
                    for key in ("timing", "oracle_timing"):
                        route = row[key].get("routing")
                        if route:
                            assert (
                                route["task"] == "rag"
                                and route["selected_model"] == "qwen-small"
                            )
            for row in suite["logs"]:
                if name != "fixed":
                    route = row["timing"]["routing"]
                    assert route["task"] == "logs"
                    assert route["selected_model"] == (
                        "qwen-large" if name == "log-experiment" else "qwen-small"
                    )
            results.append(
                {
                    "suite": suite["suite"],
                    "suite_sha256": suite["suite_sha256"],
                    "summary": suite["summary"],
                    "complete_evidence": len(supported),
                    "mean_log_seconds_including_switch": mean(
                        row["timing"]["seconds"] for row in suite["logs"]
                    ),
                }
            )
        summary["runs"][name] = {
            "report_sha256": digest(folder / f"{name}.json"),
            "seconds": report["seconds"],
            "peak_rss_mib": report["after"]["peak_rss"] / 1024**2,
            "load_seconds": [
                event["seconds"]
                for event in report["events"]
                if event["event"] == "load"
            ],
            "suites": results,
        }
    switch = reports["switch"]
    assert [r["routing"]["selected_model"] for r in switch["requests"]] == [
        "qwen-small",
        "qwen-large",
        "qwen-small",
    ]
    assert [e["event"] for e in switch["events"]] == [
        "load",
        "close",
        "load",
        "close",
        "load",
        "close",
    ]
    assert all(r["answer"].strip(". ") == "OK" for r in switch["requests"])
    assert (
        switch["fallback"]["fallback_used"]
        and switch["fallback"]["selected_model"] == "qwen-small"
    )
    summary["real_switch_and_fallback_pass"] = True
    summary["default_policy_decision"] = (
        "Keep Auto effectively single-model: no measured quality gain from the bounded 3B log experiment."
    )
    (folder / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "protected_hashes": "PASS",
                "suite_comparisons": "PASS",
                "policy_selections": "PASS",
                "real_switch_and_fallback": "PASS",
            }
        )
    )


if __name__ == "__main__":
    main()
