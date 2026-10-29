"""Model-independent coverage, qualification, polarity and scope contracts."""

import json

import pytest
from test_facts import payload, source

from app.ingestion.parser import chunk_document
from app.rag.evidence import answer_plan, blocks, status
from app.rag.retrieval import assemble_context
from app.rag.search import select_evidence
from app.services.facts import packet, render, validate_facts


@pytest.mark.parametrize("heading", ["Production", "Production / API"])
@pytest.mark.parametrize(
    "body",
    [
        "environment: production\nport: 8443",
        "- service: api\n  external_access: false\n  port: 8080",
        "Production\n- host: node-01\n- port: 443",
    ],
)
def test_qualified_records_keep_full_original_span(heading, body):
    s = source(body, heading=heading)
    slots, candidates = packet("What port does api use?", [s.model_copy(update={"service": "api"})])
    assert candidates
    for c in candidates.values():
        assert "port:" in c["quote"]
        assert len(c["quote"].splitlines()) >= 2
        assert c["quote"] in body
        assert c["line_end"] - c["line"] + 1 == len(c["quote"].splitlines())
        assert "production" in c["scopes"]


@pytest.mark.parametrize(
    "left,right",
    [
        ("production", "staging"),
        ("internal", "external"),
        ("primary", "secondary"),
        ("source", "destination"),
        ("old", "new"),
        ("spare", "main"),
        ("enabled", "disabled"),
    ],
)
def test_wrong_scope_is_not_an_alternative_even_with_identical_values(left, right):
    sources = [
        source(f"Atlas {left} port: 7210.", heading=f"Atlas {left}"),
        source(f"Atlas {right} port: 7210.", "S2", heading=f"Atlas {right}"),
    ]
    slots, candidates = packet(f"What is Atlas {left} port?", sources)
    assert candidates and {c["source"] for c in candidates.values()} == {"S1"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Atlas previously used port 7010.", "historical"),
        ("Atlas port 7010 is no longer valid.", "superseded"),
        ("The Atlas setting was removed.", "superseded"),
        ("Atlas port 7010 was disproved.", "disproved"),
        ("We initially suspected Atlas port 7010.", "hypothetical"),
        ("Atlas firmware version is not documented.", "unknown"),
        ("Atlas firmware version is unknown.", "unknown"),
        ("Do not infer the Atlas firmware version.", "unknown"),
        ("Atlas does not use port 7010.", "negated"),
        ("replication_enabled: false", "negated"),
        ("Atlas uses port 7011.", "asserted"),
    ],
)
def test_semantic_status_is_deterministic(text, expected):
    assert status(text) == expected


@pytest.mark.parametrize(
    "line",
    [
        "Atlas previously used port 7010.",
        "Atlas port 7010 is no longer valid.",
        "Atlas port 7010 was disproved.",
        "We initially suspected Atlas port 7010.",
    ],
)
def test_noncurrent_records_cannot_supply_current_values(line):
    sources = [source(line + "\nAtlas uses port 7011.")]
    slots, candidates = packet("What is Atlas current port?", sources)
    assert candidates and all("7010" not in c["value"] for c in candidates.values())
    result = validate_facts(json.dumps(payload(slots)), slots, candidates, sources)
    assert "7010" not in render(result, slots, sources)[0]


def test_unknown_limitation_never_becomes_a_factual_value():
    sources = [source("Atlas firmware version is not documented.")]
    slots, candidates = packet("What firmware version does Atlas use?", sources)
    assert not candidates
    result = validate_facts(json.dumps(payload(slots)), slots, candidates, sources)
    answer, used, claims = render(result, slots, sources)
    assert "does not specify" in answer and not used and not claims


def test_negation_preserves_exact_original_value_and_status():
    sources = [source("Atlas does not use port 7010.")]
    slots, candidates = packet("What port does Atlas use?", sources)
    assert {c["status"] for c in candidates.values()} == {"negated"}
    result = validate_facts(json.dumps(payload(slots)), slots, candidates, sources)
    assert "does not use port 7010" in render(result, slots, sources)[0]


def test_repeated_attributes_have_independent_subject_slots():
    needs = answer_plan("What is Atlas retention and Borealis retention?")
    assert len(needs) == 2
    assert needs[0].entities == {"atlas"} and needs[1].entities == {"borealis"}


def test_internal_and_published_ports_have_independent_slots():
    needs = answer_plan("What internal and host ports does Atlas use?")
    assert needs[0].facts == {"container_port", "host_port"}


def test_unlisted_attributes_get_independent_slots_without_vocabulary_changes():
    needs = answer_plan("What timeout and retry limit does Atlas use?")
    assert needs[0].facts == {"timeout", "limit_retry"}
    sources = [source("Atlas timeout: 40 seconds."), source("Atlas retry_limit: 3", "S2")]
    slots, _ = packet("What timeout and retry limit does Atlas use?", sources)
    assert len(slots) == 2 and all(s["candidates"] for s in slots)


def test_polarity_question_can_discover_a_disabled_setting():
    _, candidates = packet(
        "Is Atlas replication enabled?", [source("Atlas replication is disabled.")]
    )
    assert candidates and {c["status"] for c in candidates.values()} == {"negated"}


def test_unknown_sibling_does_not_erase_asserted_field():
    _, candidates = packet(
        "What port does Atlas use?",
        [source("service: Atlas\nfirmware_version: unknown\nport: 7010")],
    )
    assert candidates and {c["status"] for c in candidates.values()} == {"asserted"}


def test_previous_heading_is_not_current_evidence():
    _, candidates = packet(
        "What is Atlas current port?",
        [source("Atlas port: 7010", heading="Previous Atlas configuration")],
    )
    assert not candidates


async def test_only_unknown_evidence_refuses_without_calling_a_model():
    from app.services.facts import fact_answer

    class Provider:
        async def chat(self, messages):
            raise AssertionError("A limitation is not a factual extraction candidate")

    result = await fact_answer(
        Provider(),
        [source("Atlas firmware version is not documented.")],
        "What firmware version does Atlas use?",
    )
    assert result["quality"] == "insufficient_evidence"
    assert not result["grounded"] and not result["claims"]
    assert "does not specify" in result["answer"]


def test_lower_ranked_evidence_survives_independent_slot_selection():
    class Store:
        def section_neighbors(self, hit):
            return []

    hits = [
        source("Atlas port: 7010.").model_copy(update={"score": 2.0}),
        source("Atlas subnet: 192.0.2.0/25", "S2").model_copy(update={"score": 0.2}),
    ]
    selected = select_evidence(Store(), "What port and subnet does Atlas use?", hits)
    assert {h.label for h in selected} == {"S1", "S2"}


def test_budget_reserves_coverage_before_redundant_text():
    hits = [
        source("Atlas port: 7010.\n" + "Unrelated prose.\n" * 100),
        source("Atlas subnet: 192.0.2.0/25", "S2"),
    ]
    encoded, sources = assemble_context(hits, 700, "What port and subnet does Atlas use?")
    assert len(encoded) <= 700
    assert any("7010" in s.text for s in sources)
    assert any("192.0.2.0/25" in s.text for s in sources)


def test_identical_values_in_different_scopes_are_not_deduplicated():
    hits = [
        source("port: 7010", heading="Production Atlas"),
        source("port: 7010", "S2", heading="Staging Atlas"),
    ]
    _, sources = assemble_context(hits, 2000, "What are Atlas ports?")
    assert len(sources) == 2


def test_nested_yaml_scopes_never_merge():
    s = source(
        "production:\n  service: Atlas\n  port: 7010\nstaging:\n  service: Atlas\n  port: 7011"
    )
    slots, candidates = packet("What is production Atlas port?", [s])
    assert candidates
    assert all(
        "7011" not in c["quote"] and "production" in c["qualifier"] for c in candidates.values()
    )


def test_list_objects_remain_separate():
    s = source(
        "- service: Atlas\n  port: 7010\n- service: Borealis\n  port: 7011", heading="Services"
    )
    assert len(list(blocks(s))) == 2
    _, candidates = packet("What port does Atlas use?", [s])
    assert candidates and all("Borealis" not in c["quote"] for c in candidates.values())


@pytest.mark.parametrize(
    "second_heading,second_line,kind",
    [
        ("Production Atlas", "Atlas port: 7011", "conflict"),
        ("Staging Atlas", "Atlas port: 7011", "alternative"),
        ("Production Atlas", "Atlas previously used port 7011", "superseded"),
    ],
)
def test_conflict_alternative_and_superseded_are_distinct(second_heading, second_line, kind):
    sources = [
        source("Atlas port: 7010", heading="Production Atlas"),
        source(second_line, "S2", heading=second_heading),
    ]
    slots, candidates = packet("What port does Atlas use, including previous values?", sources)
    data = payload(slots)
    for item, slot in zip(data["facts"], slots, strict=True):
        item["evidence"] = [c["id"] for c in slot["candidates"]]
    result = validate_facts(json.dumps(data), slots, candidates, sources)
    assert result["conflicts"][0]["kind"] == kind


def test_accepting_one_record_does_not_silently_accept_all_lexical_candidates():
    sources = [source("Atlas port: 7010"), source("Atlas port: 7011", "S2")]
    slots, candidates = packet("What port does Atlas use?", sources)
    result = validate_facts(json.dumps(payload(slots)), slots, candidates, sources)
    assert len(result["facts"]) == 1
    assert "7011" not in render(result, slots, sources)[0]


def test_mapping_lineage_survives_large_block_chunk_fallback_and_storage(tmp_path):
    from app.services.storage import Storage

    text = "production:\n  service: Atlas\n" + "  note: filler\n" * 200 + "  port: 7010"
    chunks = chunk_document(text, "yaml", target=600)
    assert len(chunks) > 1 and "production" in chunks[-1].lineage
    store = Storage(tmp_path, "test")
    store.add_document(
        dict(
            filename="a.yaml",
            source_path="a.yaml",
            document_type="yaml",
            service="atlas",
            content_hash="a",
        ),
        chunks,
        [[1.0, 0.0]] * len(chunks),
    )
    store.close()
    store = Storage(tmp_path, "test")
    try:
        hit = store.lexical_search("port")[0]
        assert "production" in hit.lineage
    finally:
        store.close()


def test_qualified_block_larger_than_one_line_quote_limit_is_preserved():
    text = "service: Atlas\nenvironment: production\n" + "note: filler\n" * 100 + "port: 7010"
    assert 1200 < len(text) < 2400
    slots, candidates = packet("What port does production Atlas use?", [source(text)])
    assert candidates
    result = validate_facts(json.dumps(payload(slots)), slots, candidates, [source(text)])
    assert "7010" in render(result, slots, [source(text)])[0]


def test_flat_qualifiers_cross_large_record_chunk_boundaries():
    text = "service: Atlas\nenvironment: production\n" + "note: filler\n" * 220 + "port: 7010"
    chunks = chunk_document(text, "yaml", target=600)
    assert len(chunks) > 1
    assert "production" in chunks[-1].lineage and "Atlas" in chunks[-1].lineage


def test_rank_dedup_keeps_equal_values_under_different_parent_scopes(tmp_path):
    from app.ingestion.parser import Chunk
    from app.rag.search import search
    from app.services.storage import Storage

    store = Storage(tmp_path, "test")
    try:
        for scope in ("Production", "Staging"):
            store.add_document(
                dict(
                    filename=scope + ".md",
                    source_path=scope + ".md",
                    document_type="md",
                    service="atlas",
                    content_hash=scope,
                ),
                [Chunk("port: 7010", 5, 5, scope + " Atlas")],
                [[1.0, 0.0]],
            )
        hits, _ = search(store, "What port does Atlas use?", [1.0, 0.0])
        assert len(hits) == 2
    finally:
        store.close()


def test_original_assessment_artifacts_and_classifications_are_immutable():
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for path, digest in json.loads(
        (root / "docs/heldout/protected-baseline.json").read_text()
    ).items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest


def test_location_nouns_do_not_become_extra_requested_attributes():
    needs = answer_plan("Which firmware version is installed on the Atlas serial adapter?")
    assert needs[0].facts == {"version"}


def test_independent_hostname_and_metric_port_do_not_become_host_port():
    needs = answer_plan("Which host and metrics port belong to production Atlas?")
    assert needs[0].facts == {"host", "port"}


def test_new_subject_does_not_inherit_previous_entity_without_anaphora():
    needs = answer_plan("Which host runs Atlas and which host runs primary backups?")
    assert needs[0].entities == {"atlas"}
    assert "atlas" not in needs[1].entities


def test_multiword_mapping_keys_keep_service_and_environment_on_whole_record():
    s = source(
        "Service: Atlas\nEnvironment: production\nHost: node-a\n"
        "External access: disabled\nInternal port: 7010\nRetention period: 9 days",
        heading="Operations / Production group",
    )
    records = list(blocks(s))
    assert len(records) == 1 and records[0].text == s.text
    _, candidates = packet("What production Atlas internal port is documented?", [s])
    assert candidates


def test_reported_disproved_context_does_not_pollute_requested_entities():
    needs = answer_plan(
        "What internal port does Atlas actually listen on, despite the disproved ticket guess?"
    )
    assert needs[0].entities == {"atlas"}


def test_rehearsal_qualification_excludes_production_alternative():
    sources = [
        source("Atlas port: 7010", heading="Production Atlas"),
        source(
            "For the spare Atlas, use this setting only during rehearsal.\nIts port is 7011.",
            "S2",
            heading="Bench qualification",
        ),
    ]
    _, candidates = packet("What port does spare Atlas use during rehearsal?", sources)
    assert candidates and {c["source"] for c in candidates.values()} == {"S2"}


def test_depend_on_requests_relationship_instead_of_generic_service_key():
    needs = answer_plan("Which service does Atlas depend on?")
    assert needs[0].facts == {"dependency"} and needs[0].entities == {"atlas"}


def test_change_question_cannot_select_only_the_previous_value():
    _, candidates = packet(
        "What changed about production Atlas ports?",
        [
            source(
                "Atlas previously used host port 7010.\nAtlas host port changed to 7011.\n"
                "Atlas container port remained 7012."
            )
        ],
    )
    assert candidates and all(c["status"] != "historical" for c in candidates.values())


@pytest.mark.parametrize(
    "question",
    [
        "Which firmware version is installed on the Atlas serial adapter?",
        "What firmware version does Atlas on the serial adapter use?",
        "What firmware version is documented for the Atlas adapter path?",
        "Which version for the Atlas adapter path is documented?",
    ],
)
def test_request_region_does_not_extract_location_attributes(question):
    assert answer_plan(question)[0].facts == {"version"}


@pytest.mark.parametrize(
    "line",
    [
        "Atlas port: 7010",
        "Atlas container port remained unchanged at 7010.",
        "Atlas does not use port 7010.",
    ],
)
async def test_explicit_current_typed_values_cannot_disappear_with_empty_selection(line):
    from app.services.facts import fact_answer

    class Empty:
        async def chat(self, messages):
            slots = json.loads(messages[-1]["content"])["slots"]
            return json.dumps({s["id"]: [] for s in slots})

    result = await fact_answer(Empty(), [source(line)], "What port does Atlas use?")
    assert result["grounded"] and line in result["answer"]
    assert all(f["selection_origin"] == "typed_value_guard" for f in result["fact_result"]["facts"])


@pytest.mark.parametrize(
    "line",
    [
        "Atlas port is described elsewhere.",
        "Atlas port belongs to appendix 2.",
        "Atlas port was guessed to be 7010.",
        "Atlas port is not documented.",
    ],
)
async def test_nonvalues_and_guesses_cannot_be_recovered_as_typed_values(line):
    from app.services.facts import fact_answer

    class Empty:
        async def chat(self, messages):
            slots = json.loads(messages[-1]["content"])["slots"]
            return json.dumps({s["id"]: [] for s in slots})

    result = await fact_answer(Empty(), [source(line)], "What port does Atlas use?")
    assert not result["grounded"] and not result["claims"]


def test_policy_cross_reference_is_availability_evidence_not_a_retention_value():
    _, candidates = packet(
        "How long is Atlas retention?",
        [source("Atlas retention belongs to the operations handbook, not this policy.")],
    )
    assert not candidates


def selected_all(question, sources):
    slots, candidates = packet(question, sources)
    data = payload(slots)
    for item, slot in zip(data["facts"], slots, strict=True):
        item["evidence"] = [c["id"] for c in slot["candidates"]]
    return validate_facts(json.dumps(data), slots, candidates, sources)


@pytest.mark.parametrize(
    "question,first,second",
    [
        ("What port does Atlas use?", "Atlas port: 7010", "Atlas uses port 7010."),
        (
            "What is Atlas URL?",
            "Atlas URL: https://atlas.example.test.",
            "Atlas public URL is https://atlas.example.test.",
        ),
        ("What is Atlas retention?", "Atlas retention is seven days.", "Atlas retention: 7 days"),
        ("What is Atlas retention?", "Atlas retention is 1 week.", "Atlas retention: 7 days"),
    ],
)
def test_equivalent_current_values_are_not_conflicts(question, first, second):
    result = selected_all(question, [source(first), source(second, "S2")])
    assert not result["conflicts"]


def test_host_port_does_not_supply_a_hostname_slot():
    _, candidates = packet(
        "Which host runs Atlas?",
        [source("Atlas moved to host node-a.\nAtlas published host port changed to 7010.")],
    )
    assert candidates and all("7010" not in c["quote"] for c in candidates.values())


def test_negative_constraint_on_other_port_can_coexist_with_current_port():
    result = selected_all(
        "What port does Atlas use?",
        [source("Atlas does not use port 7010.\nAtlas uses port 7011.")],
    )
    assert not result["conflicts"]


def test_same_value_with_opposite_polarity_is_a_conflict():
    result = selected_all(
        "What port does Atlas use?",
        [source("Atlas does not use port 7010.\nAtlas uses port 7010.")],
    )
    assert result["conflicts"][0]["kind"] == "conflict"


def test_unspecified_scope_does_not_create_an_alternative_to_same_value():
    result = selected_all(
        "What port does Atlas use?",
        [
            source("Atlas port: 7010", heading="Production Atlas"),
            source("Atlas uses port 7010.", "S2", heading="Atlas notes"),
        ],
    )
    assert not result["conflicts"]


def test_numeric_context_is_not_a_separate_slot_and_ellipsis_inherits_scope():
    question = (
        "Production Atlas has ports 7010 and 7011: which is the container port "
        "and which is the host port?"
    )
    sources = [
        source("Atlas container port: 7010\nAtlas host port: 7011", heading="Production Atlas"),
        source("Atlas host port: 7999", "S2", heading="Staging Atlas"),
    ]
    slots, candidates = packet(question, sources)
    assert {s["field"] for s in slots} == {"container_port", "host_port"}
    assert candidates and all("7999" not in c["quote"] for c in candidates.values())


def test_fenced_config_fragment_does_not_replace_document_heading():
    records = list(
        blocks(
            source(
                "```conf\nservice.example.test {\n  backend node-a:7010\n}\n```\n"
                "Atlas URL: https://atlas.example.test.",
                heading="Operations / Production Atlas",
            )
        )
    )
    assert records[-1].heading == "Operations / Production Atlas"


def test_enable_disable_states_in_same_scope_are_conflicting_not_alternatives():
    result = selected_all(
        "Is Atlas replication enabled?",
        [
            source("Atlas replication is enabled.", heading="Production Atlas"),
            source("Atlas replication is disabled.", "S2", heading="Production Atlas"),
        ],
    )
    assert result["conflicts"][0]["kind"] == "conflict"


@pytest.mark.parametrize("heading", ["Historical Atlas", "Superseded Atlas", "Disproved Atlas"])
def test_parent_status_applies_to_negative_statements_too(heading):
    _, candidates = packet(
        "What port does Atlas currently use?",
        [source("Atlas does not use port 7010.", heading=heading)],
    )
    assert not candidates


def test_historical_negation_retains_both_temporal_status_and_polarity():
    _, candidates = packet(
        "What is Atlas historical port configuration?",
        [source("Atlas does not use port 7010.", heading="Historical Atlas")],
    )
    assert candidates
    assert all(
        c["status"] == "historical" and c["polarity"] == "negated" for c in candidates.values()
    )


@pytest.mark.parametrize("requested,other", [("primary", "backup"), ("backup", "primary")])
def test_explicit_backup_instance_role_is_separate_from_primary(requested, other):
    _, candidates = packet(
        f"What port does {requested} Atlas use?",
        [
            source("Atlas port: 7010", heading=f"{requested.title()} Atlas"),
            source("Atlas port: 7011", "S2", heading=f"{other.title()} Atlas"),
        ],
    )
    assert candidates and {c["source"] for c in candidates.values()} == {"S1"}


def test_backup_activity_does_not_override_primary_role():
    from app.rag.evidence import scopes

    assert scopes("Primary Atlas backup retention") == {"primary"}


def test_old_heading_is_historical_even_without_word_configuration():
    _, candidates = packet(
        "What is Atlas current port?", [source("Atlas port: 7010", heading="Old Atlas")]
    )
    assert not candidates


def test_previous_only_question_does_not_include_current_values():
    _, candidates = packet(
        "What was Atlas previous port?",
        [
            source("Atlas port: 7010", heading="Previous Atlas"),
            source("Atlas port: 7011", "S2", heading="Current Atlas"),
        ],
    )
    assert candidates and {c["source"] for c in candidates.values()} == {"S1"}
