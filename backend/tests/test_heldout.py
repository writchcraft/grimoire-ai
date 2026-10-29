import hashlib
import json
from pathlib import Path

import pytest

from app.evaluation import runner
from app.ingestion.parser import parse_document
from app.services.facts import fact_answer
from app.services.log_analysis import inspect_logs

ROOT = Path(__file__).resolve().parents[2]


def test_frozen_suites_and_expected_evidence():
    manifest = json.loads((ROOT / "docs/heldout/freeze.json").read_text())
    for name in ("development", "heldout"):
        path = runner.suite_path(name)
        key = path.relative_to(ROOT).as_posix()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sha256"][key]
        suite = runner.load_suite(name)
        for filename, content in suite["documents"].items():
            parse_document(filename, content.encode(), 100000)
        for case in suite["qa"]:
            for requirement in case["evidence"]:
                assert requirement["contains"] in suite["documents"][requirement["document"]]
    development, heldout = runner.load_suite(), runner.load_suite("heldout")
    assert not development["documents"].keys() & heldout["documents"].keys()
    assert len(development["qa"]) + len(development["logs"]) == 20
    assert 25 <= len(heldout["qa"]) + len(heldout["logs"]) <= 40
    assert sum(len(d) > 1000 and d.count("## ") >= 3 for d in heldout["documents"].values()) >= 3


def test_heldout_log_annotations_are_independent_and_exact():
    for case in runner.load_suite("heldout")["logs"]:
        observed = inspect_logs(case["content"])
        assert observed["error_count"] == case["errors"]
        assert observed["warning_count"] == case["warnings"]
        assert [e["line"] for e in observed["events"]] == case["events"]


async def test_sequential_suites_use_disjoint_disposable_indexes(settings, provider, monkeypatch):
    from app.services import runtime

    monkeypatch.setattr(runtime, "embedding_provider", lambda _: provider)
    paths = []
    real_ingest = runner.ingest

    async def ingest(storage, embeddings, isolated, name, *args):
        if isolated.data_dir not in paths:
            paths.append(isolated.data_dir)
        return await real_ingest(storage, embeddings, isolated, name, *args)

    monkeypatch.setattr(runner, "ingest", ingest)
    for name in ("development", "heldout", "development"):
        result = await runner.evaluate(settings, suite_name=name, retrieval_only=True)
        allowed = runner.load_suite(name)["documents"].keys()
        assert all(
            h["filename"] in allowed
            for row in result["qa"]
            for h in row["retrieval"]["ranked_chunks"]
        )
    assert len(set(paths)) == 3
    assert all(not p.exists() for p in paths)
    assert not (settings.data_dir / "grimoire.sqlite3").exists()


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"Q1":["E777"]}',
        '{"Q1":[],"injected":"S884"}',
        "{}",
        '{"Q1":"E1"}',
    ],
)
async def test_invalid_fact_selections_fail_closed(provider, raw):
    suite = runner.load_suite("heldout")
    case = suite["qa"][0]
    _, sources = runner.oracle_sources(case, suite, 8000)

    async def chat(_):
        return raw

    provider.chat = chat
    result = await fact_answer(provider, sources, case["query"])
    assert result["quality"] == "rejected_output"
    assert not result["claims"] and not result["grounded"]


def test_unknown_suite_cannot_select_a_path():
    with pytest.raises(ValueError):
        runner.load_suite("../suite")


def test_first_run_file_remains_unchanged():
    report = ROOT / "docs/heldout/first-run.json"
    expected = (ROOT / "docs/heldout/first-run.sha256").read_text().split()[0]
    assert hashlib.sha256(report.read_bytes()).hexdigest() == expected


def test_no_clobber_rejects_before_evaluation(settings, monkeypatch, capsys):
    from app import cli

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    target = settings.data_dir / "preserved.json"
    target.write_text("original")
    assert cli.main(["evaluate", "--no-clobber", "--output", str(target)]) == 1
    assert target.read_text() == "original"
    assert "already exists" in capsys.readouterr().err


async def test_cli_all_keeps_summaries_independent(settings, monkeypatch):
    from app import cli

    names = []

    async def evaluate(_, **kwargs):
        name = kwargs["suite_name"]
        names.append(name)
        return {
            "suite": name,
            "summary": {
                "retrieval": {"hit@5": {"found": 1, "total": 1}},
                "grounded_qa": {"PASS": 1} if name == "development" else {"FAIL": 1},
            },
        }

    monkeypatch.setattr(runner, "evaluate", evaluate)
    result, code = await cli.execute(
        cli.parser().parse_args(["evaluate", "--suite", "all"]), settings
    )
    assert names == ["development", "heldout"] and code == 1
    assert len(result["suites"]) == 2
