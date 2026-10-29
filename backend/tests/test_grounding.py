"""Reliability regressions use adversarial strings and tiny fakes, never weights."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.ai.native import decoding_schema
from app.ai.ollama import OllamaProvider
from app.core.errors import ProviderError
from app.evaluation.runner import load_suite, oracle_sources, retrieval_result, score_qa
from app.models import Citation
from app.services.grounding import (
    grounded_answer,
    model_events,
    validate_answer,
    validate_interpretation,
)
from app.services.log_analysis import inspect_logs


def test_native_decoding_does_not_expand_long_strings_or_unsupported_regex():
    from app.services.grounding import EvidenceAnswer

    schema = EvidenceAnswer.model_json_schema()
    decoding = decoding_schema(schema)
    assert "maxLength" not in json.dumps(decoding)
    assert "minLength" not in json.dumps(decoding)
    assert "pattern" not in json.dumps(decoding)
    assert "maxLength" in json.dumps(schema)  # validation schema remains strict
    assert decoding["required"] == ["insufficient", "claims"]
    assert decoding["additionalProperties"] is False


@pytest.fixture
def source():
    return Citation(
        id="chunk1",
        document_id="doc1",
        filename="safe.md",
        source_path="synthetic/safe.md",
        document_type="md",
        service=None,
        chunk_number=1,
        line_start=1,
        line_end=2,
        text="Grafana does not use port 9999.\nGrafana uses port 3000.",
        score=0.9,
        label="S1",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"insufficient": False, "claims": [{"source": "S99", "quote": "Grafana uses port 3000."}]},
        {"insufficient": False, "claims": [{"source": "secret.yaml", "quote": "port 3000"}]},
        {"insufficient": False, "claims": [{"source": "S1", "quote": "Grafana uses port 5432."}]},
        {"insufficient": False, "claims": [{"source": "S1", "quote": "use port 9999."}]},
        {"insufficient": False, "claims": []},
        {"insufficient": True, "claims": [{"source": "S1", "quote": "Grafana uses port 3000."}]},
        {"insufficient": "false", "claims": []},
        {"insufficient": False, "claims": [], "timeline": ["invented event"]},
    ],
)
def test_fabricated_sources_excerpts_partial_negation_and_empty_evidence_rejected(source, payload):
    with pytest.raises((ValidationError, ValueError)):
        validate_answer(json.dumps(payload), [source])


def test_claims_and_displayed_citations_trace_to_exact_supplied_lines(source):
    raw = json.dumps(
        {"insufficient": False, "claims": [{"source": "S1", "quote": "Grafana uses port 3000."}]}
    )
    answer, sources, claims = validate_answer(raw, [source])
    assert answer == "[S1] Indexed evidence:\nGrafana uses port 3000."
    assert sources == [source] and claims[0]["quote"] in source.text


@pytest.mark.parametrize("raw", ["not json", '{"insufficient":false,"claims":[', "{}"])
async def test_malformed_and_truncated_output_fails_closed(source, raw):
    class Broken:
        async def chat(self, messages):
            return raw

    result = await grounded_answer(Broken(), [], [source])
    assert result["quality"] == "rejected_output"
    assert not result["grounded"] and not result["sources"] and not result["claims"]


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_citations_never_escape_the_api_even_during_streaming(
    client, provider, indexed, stream
):
    async def invalid(messages):
        return '{"insufficient":false,"claims":[{"source":"S999","quote":"stolen"}]}'

    provider.chat = invalid
    response = client.post("/api/chat", json={"message": "Grafana port?", "stream": stream})
    assert "S999" not in response.text and "stolen" not in response.text
    if stream:
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["sources"] == []
        assert events[-1]["quality"] == "rejected_output" and not events[-1]["grounded"]
    else:
        assert response.json()["quality"] == "rejected_output"


def test_explicit_refusal_with_related_retrieval(client, provider, indexed):
    async def refuse(messages):
        return '{"insufficient":true,"claims":[]}'

    provider.chat = refuse
    result = client.post("/api/chat", json={"message": "What is Grafana's root password?"}).json()
    assert result["quality"] == "insufficient_evidence" and not result["sources"]


@pytest.mark.parametrize(
    "item",
    [
        {"kind": "timeout", "supporting_lines": [99]},
        {"kind": "timeout", "supporting_lines": [1]},
        {"kind": "restart_loop", "supporting_lines": [1, 1]},
        {"kind": "connection_failure", "supporting_lines": []},
        {"kind": "unknown", "supporting_lines": [True]},
        {"kind": "confirmed_database_crash", "supporting_lines": [1]},
        {"kind": "unknown", "supporting_lines": [1], "timestamp": "2099-01-01"},
        {"kind": "unknown", "supporting_lines": [1], "confidence": 0.99},
    ],
)
def test_invented_log_events_unsupported_patterns_and_numeric_confidence_rejected(item):
    events = inspect_logs("ERROR connection refused")["events"]
    with pytest.raises((ValidationError, ValueError)):
        validate_interpretation(json.dumps({"interpretations": [item]}), events)


def test_model_cannot_write_incident_timeline(client, provider):
    async def invented(messages):
        return json.dumps(
            {
                "interpretations": [],
                "timeline": [{"timestamp": "2099-01-01", "service": "invented-host"}],
            }
        )

    provider.chat = invented
    result = client.post(
        "/api/incidents/summarise",
        json={
            "content": "ERROR cache-demo timeout",
            "use_model": True,
        },
    ).json()
    assert result["analysis"]["quality"] == "rejected_output"
    assert "invented-host" not in result["markdown"] and "2099" not in result["markdown"]
    assert "L1 [error] ERROR cache-demo timeout" in result["markdown"]
    for heading in ("Confirmed", "Probable", "Unknown"):
        assert f"## {heading}" in result["markdown"]
    assert result["analysis"]["events"][0]["timestamp"] is None


def test_repetition_does_not_merge_different_error_codes_or_addresses():
    evidence = inspect_logs(
        "ERROR upstream code=500\nERROR upstream code=401\nERROR at 192.0.2.1\nERROR at 192.0.2.2"
    )
    assert evidence["repeated_patterns"] == []
    repeated = inspect_logs("ERROR failed pid=100\nERROR failed pid=101")
    assert repeated["repeated_patterns"][0]["count"] == 2


def test_model_event_budget_keeps_exact_raw_lines_and_valid_references():
    evidence = inspect_logs("ERROR " + "x" * 10000 + "\nERROR timeout")
    assert model_events(evidence) == [evidence["events"][1]]


def test_corpus_expected_evidence_survives_production_chunking_and_budget():
    suite = load_suite()
    for case in suite["qa"]:
        _, sources = oracle_sources(case, suite, 8000)
        result = retrieval_result(case, sources, sources)
        if case["evidence"]:
            assert result["hit"]["5"] and result["context_complete"], case["id"]


def test_rank_metrics_require_all_sources_and_required_chunk_content():
    suite = load_suite()
    case = next(c for c in suite["qa"] if c["id"] == "multi-route")
    _, sources = oracle_sources(case, suite, 8000)
    result = retrieval_result(case, sources, sources)
    assert not result["hit"]["1"] and result["hit"]["3"]
    assert not retrieval_result(case, sources[:1], sources[:1])["context_complete"]


def test_model_output_rejection_is_not_counted_as_a_successful_unsupported_answer():
    result = score_qa(
        {"evidence": []},
        {"answer": "Rejected", "grounded": False, "claims": [], "quality": "rejected_output"},
    )
    assert result["status"] == "WARN" and result["safe_refusal"]


def test_traceable_but_irrelevant_excerpts_fail_unsupported_question_scoring(source):
    result = score_qa(
        {"evidence": []},
        {
            "answer": "Grafana uses port 3000.",
            "grounded": True,
            "claims": [{"source": "S1", "quote": "Grafana uses port 3000."}],
            "quality": "validated",
        },
    )
    assert result["status"] == "FAIL" and not result["safe_refusal"]


def test_citing_the_correct_document_without_all_required_evidence_is_a_failure():
    suite = load_suite()
    case = next(c for c in suite["qa"] if c["id"] == "database-config")
    _, sources = oracle_sources(case, suite, 8000)
    raw = json.dumps(
        {
            "insufficient": False,
            "claims": [
                {
                    "source": "S1",
                    "quote": "PostgreSQL listens on TCP port 5432 on db-demo at 192.0.2.30.",
                }
            ],
        }
    )
    answer, selected, claims = validate_answer(raw, sources)
    result = score_qa(
        case,
        {
            "answer": answer,
            "sources": selected,
            "claims": claims,
            "quality": "validated",
            "grounded": True,
        },
    )
    assert result["required_sources_selected"] and result["citation_traceable"]
    assert result["status"] == "FAIL" and not result["required_evidence_selected"]


async def test_ollama_schema_and_task_limit_and_truncation(settings):
    def handle(request):
        payload = json.loads(request.content)
        assert payload["format"] == {"type": "object"}
        assert payload["options"]["num_predict"] == 512
        return httpx.Response(
            200, json={"message": {"content": "partial"}, "done_reason": "length"}
        )

    runtime = OllamaProvider(settings, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ProviderError, match="output limit"):
            await runtime.chat_json([], {"type": "object"}, 512)
        assert runtime.last_generation["finish_reason"] == "length"
    finally:
        await runtime.close()


async def test_evaluation_exercises_real_ingestion_in_an_isolated_index(
    settings, provider, monkeypatch
):
    from app.evaluation import runner
    from app.services import runtime

    suite = load_suite()
    minimal = {
        **suite,
        "documents": {"monitoring.md": suite["documents"]["monitoring.md"]},
        "qa": [suite["qa"][0]],
        "logs": [suite["logs"][0]],
    }
    monkeypatch.setattr(runner, "load_suite", lambda: minimal)
    monkeypatch.setattr(runtime, "embedding_provider", lambda _: provider)
    monkeypatch.setattr(runtime, "generation_provider", lambda _: provider)

    async def chat(messages):
        if "Classify the supplied log events" in messages[0]["content"]:
            return '{"interpretations":[{"kind":"connection_failure","supporting_lines":[2]}]}'
        slots = json.loads(messages[-1]["content"])["slots"]
        return json.dumps({s["id"]: [s["candidates"][0]["id"]] for s in slots})

    provider.chat = chat
    result = await runner.evaluate(settings, generation_runtime=provider)
    assert result["summary"]["grounded_qa"] == {"PASS": 1}
    assert result["summary"]["oracle_qa"] == {"PASS": 1}
    assert result["summary"]["logs"] == {"PASS": 1}
    assert result["summary"]["incidents"] == {"PASS": 1}
    assert result["qa"][0]["failure_origin"] == []
    assert not (settings.data_dir / "grimoire.sqlite3").exists()


def test_cli_evaluation_json_output_and_diagnostic_hit1_are_not_a_failure(
    capsys, settings, monkeypatch
):
    from app import cli
    from app.evaluation import runner

    async def evaluate(*args, **kwargs):
        return {
            "suite_version": 1,
            "limitations": "Test fixture",
            "summary": {
                "retrieval": {"hit@1": {"found": 0, "total": 1}, "hit@5": {"found": 1, "total": 1}},
                "grounded_qa": {"PASS": 1},
            },
        }

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(runner, "evaluate", evaluate)
    output = settings.data_dir / "report.json"
    assert cli.main(["evaluate", "--json", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(output.read_text())
    assert cli.main(["evaluate"]) == 0
    assert "grounded_qa" in capsys.readouterr().out
