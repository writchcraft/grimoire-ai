import json

import pytest

from app.ingestion.parser import Chunk, chunk_document
from app.models import Citation, SearchHit
from app.rag.retrieval import assemble_context, prepare_chat
from app.rag.search import (
    claims_complete,
    complete,
    near_duplicate,
    plan,
    search,
    select_evidence,
    tokens,
)
from app.services.grounding import grounded_answer
from app.services.storage import Storage


def hit(text, **values):
    return SearchHit(
        id="a",
        document_id="doc",
        filename="atlas.md",
        source_path="atlas.md",
        document_type="md",
        service=None,
        chunk_number=1,
        line_start=1,
        line_end=len(text.splitlines()),
        text=text,
        score=1,
        **values,
    )


def add(store, name, text, vector=(1.0, 0.0), kind="md", service=None):
    return store.add_document(
        dict(
            filename=name, source_path=name, document_type=kind, service=service, content_hash=name
        ),
        chunk_document(text, kind),
        [list(vector)] * len(chunk_document(text, kind)),
    )


def test_heading_metadata_is_inherited_without_fabricating_source_lines():
    text = "# Atlas\n" + "A paragraph. " * 100 + "\n\n" + "port: 8123\n" * 60
    chunks = chunk_document(text, "md", target=700)
    assert len(chunks) >= 2
    assert all(c.heading == "Atlas" for c in chunks)
    lines = text.splitlines()
    assert all(c.text == "\n".join(lines[c.line_start - 1 : c.line_end]) for c in chunks)


@pytest.mark.parametrize("kind", ["yaml", "json", "txt", "log"])
def test_formats_preserve_bounded_blocks_and_line_provenance(kind):
    if kind == "yaml":
        text = (
            "services:\n  atlas:\n    image: atlas\n    ports:\n      - '8123:8123'\n"
            "  borealis:\n    image: borealis\n    port: 9001"
        )
    elif kind == "json":
        text = json.dumps(
            {
                "services": {
                    "atlas": {"image": "atlas", "ports": [8123]},
                    "borealis": {"image": "borealis", "port": 9001},
                }
            },
            indent=2,
        )
    elif kind == "txt":
        text = "[atlas]\nhost=atlas-node\nport=8123\n\n[borealis]\nport=9001"
    else:
        text = "\n".join(f"2026-01-01T00:00:00Z ERROR event={i}" for i in range(120))
    chunks = chunk_document(text, kind, target=60)
    assert all(len(c.text) <= 2400 for c in chunks)
    assert "\n".join(c.text for c in chunks) == text
    if kind != "log":
        assert any("atlas" in c.text and "8123" in c.text for c in chunks)


def test_fence_and_adjacent_explanation_are_kept_when_within_limit():
    text = (
        "# Atlas\nExplanation of configuration.\n\n```yaml\nport: 8123\n"
        "# not a heading\n```\nScope remains local."
    )
    assert len(chunk_document(text, "md")) == 1


def test_exact_identifiers_and_limited_variants():
    assert {"atlas-node", "192.0.2.40", "server.port", "retention"} <= tokens(
        "ATLAS-NODE, 192.0.2.40 server.port backups retained"
    )
    assert tokens("ports published") == {"port", "publish"}


def test_keyword_rescues_below_vector_threshold_and_filters_before_ranking(tmp_path):
    with_store = Storage(tmp_path, "test")
    try:
        add(with_store, "a.md", "# Atlas\nAtlas port: 8123", (0.0, 1.0), service="atlas")
        add(with_store, "b.md", "# Borealis\nBorealis port: 8124", service="borealis")
        hits, debug = search(with_store, "What port does Atlas use?", [1.0, 0.0], service="atlas")
        assert [h.filename for h in hits] == ["a.md"]
        assert debug["components"][hits[0].id]["vector"] == 0
        assert debug["service_filter"] == "atlas"
    finally:
        with_store.close()


def test_scoped_exact_entity_ranks_ahead_of_vector_distractor(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(store, "staging.md", "# Staging\nAtlas host port: 9100", (1.0, 0.0))
        add(store, "live.md", "# Production\nAtlas host port: 8123", (0.0, 1.0))
        hits, _ = search(
            store, "What is production Atlas host port, excluding staging?", [1.0, 0.0]
        )
        assert hits[0].filename == "live.md"
    finally:
        store.close()


def test_multipart_collects_distinct_sources_and_refuses_missing_part(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(store, "placement.md", "# Atlas\nAtlas runs on host atlas-node.")
        add(store, "network.md", "# Atlas\nAtlas exposes port 8123.")
        query = "What host runs Atlas and what port does it expose?"
        needs = plan(query)
        assert len(needs) == 2 and needs[1].entities == {"atlas"}
        hits, _ = search(store, query, [1.0, 0.0])
        selected = select_evidence(store, query, hits)
        assert len(selected) == 2 and complete(query, selected)
        assert not select_evidence(store, query, hits[:1])
    finally:
        store.close()


def test_near_duplicates_keep_different_values_negations_and_scope():
    assert near_duplicate("Atlas port 8123.\n", "Atlas   port 8123.")
    assert not near_duplicate("Atlas port 8123.", "Atlas port 8124.")
    assert not near_duplicate("Atlas is enabled.", "Atlas is not enabled.")
    assert not near_duplicate("production Atlas port 8123", "staging Atlas port 8123")


def test_expansion_cannot_cross_heading_and_retains_exact_siblings(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        chunks = [
            Chunk("# Atlas\nAtlas host: atlas-node", 1, 2, "Atlas"),
            Chunk("port: 8123", 3, 3, "Atlas"),
            Chunk("# Borealis\nport: 9100", 4, 5, "Borealis"),
        ]
        store.add_document(
            dict(
                filename="a.md",
                source_path="a.md",
                document_type="md",
                service=None,
                content_hash="a",
            ),
            chunks,
            [[1.0, 0.0]] * 3,
        )
        hits = store.semantic_search([1.0, 0.0], 3, 0)
        anchor = next(h for h in hits if h.chunk_number == 1)
        siblings = store.section_neighbors(anchor)
        assert [h.chunk_number for h in siblings] == [2]
        selected = select_evidence(store, "What port does Atlas use?", [anchor])
        assert any(h.text == "port: 8123" for h in selected)
    finally:
        store.close()


def test_budget_dedup_and_multipart_completeness():
    a = hit("Atlas host: atlas-node")
    b = a.model_copy(update={"id": "b", "text": "Atlas port: 8123"})
    encoded, sources = assemble_context([a, a, b], 1000)
    assert len(sources) == 2 and len(encoded) <= 1000
    assert complete("What host runs Atlas and what port does it expose?", sources)
    assert not complete("What host runs Atlas and what port does it expose?", sources[:1])
    encoded, _ = assemble_context([a, b], len(assemble_context([a], 1000)[0]))
    assert len(json.loads(encoded)) == 1


def test_claims_require_each_requested_attribute_even_with_complete_sources():
    s = Citation(
        **hit("Atlas container port: 8123\nAtlas host port: 8124").model_dump(), label="S1"
    )
    query = "What are Atlas container and host ports?"
    claims = [{"source": "S1", "quote": "Atlas container port: 8123"}]
    assert not claims_complete(query, claims, [s])
    claims.append({"source": "S1", "quote": "Atlas host port: 8124"})
    assert claims_complete(query, claims, [s])


async def test_partial_model_answer_returns_insufficient_evidence():
    class Partial:
        async def chat(self, messages):
            return json.dumps(
                {"insufficient": False, "claims": [{"source": "S1", "quote": "Atlas port: 8123"}]}
            )

    s = Citation(**hit("Atlas port: 8123\nAtlas subnet: 192.0.2.0/24").model_dump(), label="S1")
    result = await grounded_answer(Partial(), [], [s], "What port and subnet does Atlas use?")
    assert result["quality"] == "insufficient_evidence" and not result["claims"]


async def test_weak_or_missing_entity_does_not_call_generation(settings, provider, tmp_path):
    from app.models import ChatRequest

    store = Storage(tmp_path, "test")
    try:
        add(store, "a.md", "# Atlas\nAtlas port: 8123", vector=(0.0, 1.0, 0.0))
        messages, sources = await prepare_chat(
            ChatRequest(message="What port does Borealis use?"), store, provider, settings
        )
        assert messages == [] and sources == []
        assert provider.calls == []
    finally:
        store.close()


def test_duplicate_documents_do_not_fill_ranked_results(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(store, "a.md", "# Atlas\nAtlas port: 8123")
        add(store, "copy.md", "# Atlas\nAtlas port: 8123")
        add(store, "placement.md", "# Atlas\nAtlas host: atlas-node")
        hits, _ = search(store, "What port and host does Atlas use?", [1.0, 0.0])
        assert len(hits) == 2
        assert any("atlas-node" in h.text for h in hits)
    finally:
        store.close()


def test_close_scores_allow_source_diversity_without_capping_dominant_source(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(
            store,
            "a.md",
            "# One\nAtlas port 8121\n# Two\nAtlas port 8122\n"
            "# Three\nAtlas port 8123\n# Four\nAtlas port 8124",
        )
        add(store, "b.md", "# Five\nAtlas port 8125")
        hits, _ = search(store, "Atlas port", [1.0, 0.0])
        assert any(h.filename == "b.md" for h in hits[:3])
    finally:
        store.close()


def test_each_clause_selects_its_own_best_evidence(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(
            store,
            "proxy.md",
            "# Production proxy\nThe proxy runs on host edge-node.\n"
            "The proxy routes Atlas host port 8123.",
        )
        add(store, "atlas.md", "# Production Atlas\nAtlas publishes host port 8123.")
        query = "Which host runs the proxy and what host port does production Atlas publish?"
        hits, _ = search(store, query, [1.0, 0.0])
        selected = select_evidence(store, query, hits)
        assert {h.filename for h in selected} == {"proxy.md", "atlas.md"}
    finally:
        store.close()


def test_exact_service_metadata_can_identify_unnamed_config_chunk(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(store, "settings.yaml", "port: 8123", vector=(0.0, 1.0), kind="yaml", service="atlas")
        hits, _ = search(store, "What port does Atlas use?", [1.0, 0.0])
        selected = select_evidence(store, "What port does Atlas use?", hits)
        assert len(selected) == 1 and selected[0].service == "atlas"
        _, sources = assemble_context(selected, 2000)
        assert claims_complete(
            "What port does Atlas use?", [{"source": "S1", "quote": "port: 8123"}], sources
        )
    finally:
        store.close()


def test_normalized_lexical_candidates_keep_surface_inflections(tmp_path):
    store = Storage(tmp_path, "test")
    try:
        add(store, "x.txt", "Backups retained here.", vector=(0.0, 1.0), kind="txt")
        hits, _ = search(store, "backup retention", [1.0, 0.0])
        assert hits and hits[0].filename == "x.txt"
    finally:
        store.close()


def test_near_duplicate_keeps_different_hostnames_in_long_config():
    body = "Common explanatory context. " * 100
    assert not near_duplicate(body + "host: alpha-node", body + "host: beta-node")


def test_heading_metadata_survives_persisted_index_rebuild(tmp_path):
    store = Storage(tmp_path, "test")
    add(store, "a.md", "# Atlas\nport: 8123")
    store.close()
    reopened = Storage(tmp_path, "test")
    try:
        hits, _ = search(reopened, "What port does Atlas use?", [1.0, 0.0])
        assert hits[0].heading == "Atlas"
    finally:
        reopened.close()
