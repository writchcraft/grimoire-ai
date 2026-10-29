"""Fact contract regressions: application-owned values and explicit answer slots."""

import json

import pytest
from pydantic import ValidationError

from app.models import Citation
from app.services.facts import packet, render, validate_facts
from app.services.grounding import grounded_answer


def source(text, label="S1", heading="Atlas"):
    return Citation(
        id=label,
        document_id=label,
        filename="config.md",
        source_path="demo/config.md",
        document_type="md",
        service="atlas",
        chunk_number=1,
        line_start=1,
        line_end=len(text.splitlines()),
        text=text,
        score=1.0,
        label=label,
        heading=heading,
    )


def payload(slots):
    return {
        "facts": [
            {"slot": s["id"], "evidence": [s["candidates"][0]["id"]]}
            for s in slots
            if s["candidates"]
        ],
        "unknowns": [s["id"] for s in slots if not s["candidates"]],
    }


def validate(data, slots, candidates, sources):
    return validate_facts(json.dumps(data), slots, candidates, sources)


def test_multipart_fields_are_independent_and_cannot_be_omitted():
    sources = [source("Atlas uses port 8123.\nAtlas allows subnet 198.51.100.0/24.")]
    slots, candidates = packet("What port and subnet does Atlas use?", sources)
    assert {s["field"] for s in slots} == {"port", "subnet"}
    data = payload(slots)
    data["facts"].pop()
    with pytest.raises(ValueError, match="Missing required"):
        validate(data, slots, candidates, sources)
    result = validate(payload(slots), slots, candidates, sources)
    answer, _, claims = render(result, slots, sources)
    assert "8123" in answer and "198.51.100.0/24" in answer and len(claims) == 2


@pytest.mark.parametrize("mutation", ["invented_id", "wrong_slot", "excerpt", "value", "duplicate"])
def test_invented_excerpts_values_and_invalid_combinations_fail_closed(mutation):
    sources = [source("Atlas uses port 8123.\nAtlas allows subnet 198.51.100.0/24.")]
    slots, candidates = packet("What port and subnet does Atlas use?", sources)
    data = payload(slots)
    if mutation == "invented_id":
        data["facts"][0]["evidence"] = ["S99"]
    elif mutation == "wrong_slot":
        data["facts"][0]["evidence"] = data["facts"][1]["evidence"]
    elif mutation in {"excerpt", "value"}:
        data["facts"][0][mutation] = "Atlas uses port 9999."
    else:
        data["facts"].append(data["facts"][0])
    with pytest.raises((ValueError, ValidationError)):
        validate(data, slots, candidates, sources)


@pytest.mark.parametrize(
    "line",
    [
        "Atlas port remained 8123.",
        "Atlas does not use port 9999.",
        "Atlas feature is disabled.",
        "Atlas service was not moved.",
        "Atlas route was not modified.",
        "Atlas configuration is explicitly absent.",
    ],
)
def test_unchanged_negative_and_absent_records_are_never_rewritten(line):
    sources = [source(line)]
    question = "What port does Atlas use?" if "port" in line else "What is documented for Atlas?"
    slots, candidates = packet(question, sources)
    result = validate(payload(slots), slots, candidates, sources)
    answer, _, claims = render(result, slots, sources)
    assert line in answer and claims == [{"source": "S1", "quote": line}]
    assert result["facts"][0]["value"] == line


def test_change_slots_include_unchanged_sibling_port():
    sources = [source("Atlas host port changed to 8124.\nThe container port remained 8123.")]
    slots, candidates = packet("What changed about Atlas ports?", sources)
    assert {s["field"] for s in slots} == {"host_port", "container_port"}
    result = validate(payload(slots), slots, candidates, sources)
    assert "remained 8123" in render(result, slots, sources)[0]


def test_conflicting_records_survive_selection_without_synthetic_configuration():
    sources = [
        source("Atlas uses port 8123.", heading="Staging Atlas"),
        source("Atlas uses port 8124.", "S2", "Production Atlas"),
    ]
    slots, candidates = packet("What port does Atlas use?", sources)
    data = payload(slots)
    for item, slot in zip(data["facts"], slots, strict=True):
        item["evidence"] = [c["id"] for c in slot["candidates"]]
    result = validate(data, slots, candidates, sources)
    answer, used, _ = render(result, slots, sources)
    assert len(result["conflicts"]) == 1 and len(used) == 2
    assert "[S1] (Staging Atlas) Atlas uses port 8123." in answer
    assert "[S2] (Production Atlas) Atlas uses port 8124." in answer
    scoped, _ = packet("What production port does Atlas use, excluding staging?", sources)
    assert {c["source"] for s in scoped for c in s["candidates"]} == {"S2"}


async def test_partial_answer_displays_unknown_instead_of_silently_omitting_it():
    sources = [source("Atlas uses port 8123.")]

    class Provider:
        async def chat(self, messages):
            slots = json.loads(messages[-1]["content"])["slots"]
            return json.dumps(
                {s["id"]: [s["candidates"][0]["id"]] if s["candidates"] else [] for s in slots}
            )

    result = await grounded_answer(Provider(), [], sources, "What port and subnet does Atlas use?")
    assert result["grounded"] and "8123" in result["answer"]
    assert "Unknown (subnet)" in result["answer"]
    assert result["fact_result"]["unknowns"]
    assert result["fact_timing"]["composition_seconds"] >= 0


async def test_malformed_output_never_escapes_and_is_not_retried():
    class Provider:
        calls = 0

        async def chat(self, messages):
            self.calls += 1
            return '{"facts":[{"slot":"Q1","evidence":["secret.yaml"]}],"unknowns":[]}'

    provider = Provider()
    result = await grounded_answer(provider, [], [source("Atlas port 8123.")], "Atlas port?")
    assert result["quality"] == "rejected_output" and not result["claims"]
    assert "secret.yaml" not in result["answer"] and provider.calls == 1


def test_decoding_requires_each_slot_and_binds_its_ids():
    from app.services.facts import selection_schema

    sources = [source("Atlas port 8123.\nAtlas subnet 198.51.100.0/24.")]
    slots, _ = packet("What port and subnet does Atlas use?", sources)
    schema = selection_schema(slots)
    assert schema.model_json_schema()["required"] == ["Q1", "Q2"]
    for data in ({"Q1": ["E1"]}, {"Q1": ["E2"], "Q2": ["E1"]}, {"Q1": ["secret.yaml"], "Q2": []}):
        with pytest.raises(ValidationError):
            schema.model_validate(data)
    assert schema.model_validate({"Q1": ["E1"], "Q2": []}).model_dump()["Q2"] == []


async def test_partial_evidence_reaches_answering_through_production_retrieval(settings, provider):
    from app.ingestion.pipeline import ingest
    from app.models import ChatRequest
    from app.rag.retrieval import prepare_chat
    from app.services.storage import Storage

    store = Storage(settings.data_dir, "test")
    try:
        await ingest(
            store, provider, settings, "atlas.md", b"# Atlas\nAtlas port 8123.", "demo/atlas.md"
        )
        query = "What port and subnet does Atlas use?"
        messages, sources = await prepare_chat(
            ChatRequest(message=query), store, provider, settings
        )
        assert messages and sources
        result = await grounded_answer(provider, messages, sources, query)
        assert result["grounded"] and "Unknown (subnet)" in result["answer"]
        assert "8123" in result["answer"]
    finally:
        store.close()
