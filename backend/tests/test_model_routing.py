import asyncio
import json

import pytest
from pydantic import ValidationError

from app.ai.catalog import GenerationModel, ModelRegistry, load_registry
from app.ai.routing import RoutedProvider, routing_scope
from app.core.errors import ProviderError
from app.services.runtime import embedding_identity


@pytest.fixture
def catalog(tmp_path, settings):
    for name in ("small", "large"):
        (tmp_path / f"{name}.gguf").write_bytes(b"GGUFtest")
    data = {
        "models": [
            dict(
                id=name,
                display_name=name,
                model_path=f"{name}.gguf",
                capabilities=["general", "rag", "logs", "incidents", "config_reasoning"],
                context_size=4096,
                output_limit=512,
                resource_class=resource,
            )
            for name, resource in (("small", "light"), ("large", "medium"))
        ],
        "routing": {
            "default": "small",
            "policies": {"config_reasoning": "large"},
            "fallbacks": {"large": ["small"]},
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(data))
    return settings.model_copy(update={"model_registry_path": path}), data


def save(catalog):
    settings, data = catalog
    settings.model_registry_path.write_text(json.dumps(data))
    return settings


def test_registry_relative_paths_and_embedding_identity(catalog, settings):
    configured, _ = catalog
    registry = load_registry(configured)
    assert registry.get("small").model_path.is_file()
    assert registry.get("small").availability() == "unverified"
    assert embedding_identity(configured) == embedding_identity(settings)
    assert registry.candidates("config_reasoning") == ["large", "small"]
    assert registry.candidates("rag") == ["small"]
    registry.routing.fallbacks["small"] = ["large", "small"]
    registry.routing.fallbacks["large"] = ["small", "large", "small"]
    assert registry.candidates("config_reasoning") == ["large", "small"]


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "cloud"},
        {"capabilities": ["embedding"]},
        {"output_limit": 4096},
        {"context_size": 2},
        {"resource_class": "gigantic"},
        {"id": "bad id"},
        {"model_path": None},
        {"model_name": "unexpected"},
        {"readiness": "ready"},
    ],
)
def test_invalid_model(catalog, change):
    model = catalog[1]["models"][0] | change
    with pytest.raises(ValidationError):
        GenerationModel.model_validate(model)


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "unknown_default",
        "unknown_policy",
        "unknown_fallback",
        "capability",
        "embedding",
    ],
)
def test_invalid_registry(catalog, kind):
    _, data = catalog
    if kind == "duplicate":
        data["models"].append(data["models"][0])
    elif kind == "unknown_default":
        data["routing"]["default"] = "absent"
    elif kind == "unknown_policy":
        data["routing"]["policies"]["logs"] = "absent"
    elif kind == "unknown_fallback":
        data["routing"]["fallbacks"]["large"] = ["absent"]
    elif kind == "capability":
        data["models"][1]["capabilities"] = ["general"]
    else:
        data["models"][1]["role"] = "embedding"
    with pytest.raises(ValidationError):
        ModelRegistry.model_validate(data)


class FakeRuntime:
    def __init__(self, settings, events, fail=False):
        self.id = settings.native_model_path.stem
        self.events = events
        self.fail = fail
        self.state = "ready"
        self.settings = settings

    async def start(self):
        self.events.append(("load", self.id))

    async def health(self):
        return {
            "state": "failed" if self.fail else self.state,
            "provider": "native",
            "model": self.id,
            "backend": "fake",
            "reason": None,
        }

    async def close(self):
        self.events.append(("close", self.id))

    async def chat(self, messages):
        self.events.append(("chat", self.id))
        await asyncio.sleep(0)
        return self.id

    async def chat_json(self, messages, schema, max_tokens):
        self.events.append(("limit", max_tokens))
        return '{"insufficient":false,"claims":[{"source":"S99","quote":"invented"}]}'

    async def stream(self, messages):
        yield self.id
        await asyncio.sleep(0)
        yield self.id


def runtime(catalog, fail=False):
    events = []
    router = RoutedProvider(
        save(catalog),
        lambda s: FakeRuntime(s, events, fail and s.native_model_path.stem == "large"),
    )
    return router, events


async def test_lazy_switch_and_switch_back(catalog):
    router, events = runtime(catalog)
    await router.start()
    assert events == []
    try:
        assert await router.chat([]) == "small"
        with routing_scope("config_reasoning"):
            assert await router.chat([]) == "large"
            assert router.last_route["policy_rule"] == "explicit task policy"
        assert await router.chat([]) == "small"
        assert events == [
            ("load", "small"),
            ("chat", "small"),
            ("close", "small"),
            ("load", "large"),
            ("chat", "large"),
            ("close", "large"),
            ("load", "small"),
            ("chat", "small"),
        ]
    finally:
        await router.close()


@pytest.mark.parametrize(
    "state", ["missing", "disabled", "failed", "incompatible", "loading", "load_failure"]
)
async def test_bounded_observable_fallback(catalog, state):
    settings, data = catalog
    model = data["models"][1]
    if state == "missing":
        model["model_path"] = "absent.gguf"
    elif state == "disabled":
        model["enabled"] = False
    elif state != "load_failure":
        model["readiness"] = state
    router, events = runtime(catalog, fail=state == "load_failure")
    try:
        with routing_scope("config_reasoning"):
            assert await router.chat([]) == "small"
        assert router.last_route["fallback_used"]
        assert router.last_route["attempts"][0]["model"] == "large"
        assert len([e for e in events if e[0] == "load"]) <= 2
    finally:
        await router.close()


async def test_no_arbitrary_fallback(catalog):
    catalog[1]["models"][0]["enabled"] = False
    router, events = runtime(catalog)
    try:
        with pytest.raises(ProviderError, match="No allowed"):
            await router.chat([])
        assert not events  # A usable large model exists, but it is not an allowed fallback.
        with routing_scope("general", "unknown"):
            with pytest.raises(ValueError, match="Unknown"):
                await router.chat([])
    finally:
        await router.close()


async def test_manual_override_and_output_limit(catalog):
    router, events = runtime(catalog)
    try:
        with routing_scope("rag", "large"):
            await router.chat_json([], {}, 999)
        assert router.last_route["selected_model"] == "large"
        assert router.last_route["policy_rule"] == "manual override"
        assert ("limit", 512) in events
    finally:
        await router.close()


async def test_capability_matching_even_manual(catalog):
    catalog[1]["models"][1]["capabilities"] = ["config_reasoning"]
    router, _ = runtime(catalog)
    try:
        with routing_scope("rag", "large"):
            assert await router.chat([]) == "small"
        assert router.last_route["attempts"][0]["readiness"] == "incapable"
    finally:
        await router.close()


async def test_concurrent_stream_switch_is_serialized(catalog):
    router, events = runtime(catalog)

    async def stream():
        with routing_scope("general", "large"):
            return [v async for v in router.stream([])]

    try:
        values, value = await asyncio.gather(stream(), router.chat([]))
        assert values == ["large", "large"] and value == "small"
        assert events.index(("close", "large")) < events.index(("load", "small"))
    finally:
        await router.close()


async def test_same_global_validator_rejects_routed_invention(catalog):
    from app.services.grounding import direct_answer

    router, _ = runtime(catalog)
    try:
        with routing_scope("rag", "large"):
            result = await direct_answer(router, [], [])
        assert result["quality"] == "rejected_output"
        assert not result["claims"] and not result["sources"]
    finally:
        await router.close()


def test_cli_registry_json(catalog, monkeypatch, capsys):
    from app import cli

    monkeypatch.setattr(cli, "Settings", lambda: save(catalog))
    assert cli.main(["models", "list", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in result["generation_models"]] == ["small", "large"]
    assert cli.main(["models", "inspect", "large", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["generation_models"][0]["resource_class"] == "medium"
    assert cli.main(["models", "policy", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["default"] == "small"


@pytest.mark.parametrize("stream", [False, True])
def test_routed_api_preserves_fact_validation(catalog, provider, stream):
    from fastapi.testclient import TestClient

    from app.main import create_app

    router, _ = runtime(catalog)
    with TestClient(
        create_app(catalog[0], router, provider), base_url="http://localhost"
    ) as client:
        assert (
            client.post(
                "/api/documents/ingest",
                json={"filename": "grafana.md", "content": "Grafana is exposed on port 3000."},
            ).status_code
            == 201
        )
        response = client.post(
            "/api/chat",
            json={
                "message": "What port does Grafana use?",
                "model": "large",
                "debug": True,
                "stream": stream,
            },
        )
        assert response.status_code == 200
        data = (
            next(
                row for row in map(json.loads, response.text.splitlines()) if row["type"] == "done"
            )
            if stream
            else response.json()
        )
        assert data["quality"] == "rejected_output"
        assert data["routing"]["selected_model"] == "large"
        assert data["routing"]["task"] == "rag"
        assert "invented" not in response.text
        assert (
            client.post("/api/chat", json={"message": "Hello", "model": "unknown"}).status_code
            == 422
        )


@pytest.mark.parametrize(
    "endpoint,task", [("logs/analyse", "logs"), ("incidents/summarise", "incidents")]
)
def test_routed_api_preserves_log_validator(catalog, provider, endpoint, task):
    from fastapi.testclient import TestClient

    from app.main import create_app

    router, _ = runtime(catalog)
    with TestClient(
        create_app(catalog[0], router, provider), base_url="http://localhost"
    ) as client:
        response = client.post(
            "/api/" + endpoint,
            json={
                "content": "ERROR connection refused",
                "use_model": True,
                "model": "large",
                "debug": True,
            },
        )
        assert response.status_code == 200
        result = response.json()
        result = result.get("analysis", result)
        assert result["quality"] == "rejected_output"
        assert result["routing"]["task"] == task
        assert result["routing"]["selected_model"] == "large"


def test_duplicate_yaml_keys_rejected(catalog):
    settings, _ = catalog
    settings.model_registry_path.write_text("models: []\nmodels: []\nrouting: {default: absent}\n")
    with pytest.raises(ValueError, match="Duplicate"):
        load_registry(settings)


async def test_runtime_becomes_unready_falls_back(catalog):
    router, _ = runtime(catalog)
    try:
        with routing_scope("general", "large"):
            await router.chat([])
            router.active.state = "loading"
            assert await router.chat([]) == "small"
            assert router.last_route["attempts"] == [{"model": "large", "readiness": "loading"}]
    finally:
        await router.close()


async def test_cancelled_load_drains_before_switch(catalog):
    events = []
    loaded = asyncio.Event()
    release = asyncio.Event()

    class SlowRuntime(FakeRuntime):
        async def start(self):
            async def load():
                loaded.set()
                await release.wait()
                events.append(("loaded", self.id))

            self.loading = asyncio.create_task(load())

        async def close(self):
            await self.loading
            await super().close()

    router = RoutedProvider(save(catalog), lambda s: SlowRuntime(s, events))
    task = asyncio.create_task(router.chat([]))
    await loaded.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert events == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [("loaded", "small"), ("close", "small")]
    assert router.active is None
    await router.close()


async def test_ollama_unload_failure_aborts_switch(catalog):
    events = []

    class UnloadRuntime(FakeRuntime):
        async def unload(self):
            raise ProviderError("Unload failed")

    router = RoutedProvider(save(catalog), lambda s: UnloadRuntime(s, events))
    await router.chat([])
    with routing_scope("general", "large"):
        with pytest.raises(ProviderError, match="Unload failed"):
            await router.chat([])
    assert events == [("load", "small"), ("chat", "small")]

    async def unload():
        events.append(("unload", "small"))

    router.active.unload = unload
    await router.close()
    assert events[-2:] == [("unload", "small"), ("close", "small")]


async def test_unconfigured_legacy_never_loads_incidental_file(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unconfigured.gguf").write_bytes(b"GGUFtest")
    router = RoutedProvider(settings, factory=lambda _: pytest.fail("No model was configured"))
    with pytest.raises(ProviderError):
        await router.chat([])
    assert (await router.health())["state"] == "not_configured"
    await router.close()


def test_malformed_yaml_is_clear_configuration_error(catalog):
    settings, _ = catalog
    settings.model_registry_path.write_text("models: [\n")
    with pytest.raises(ValueError, match="Invalid generation registry"):
        load_registry(settings)


async def test_general_cli_override_needs_no_embeddings_or_index(catalog, monkeypatch):
    from app import cli
    from app.services import runtime as resources

    events = []

    class InjectedRouter(RoutedProvider):
        def __init__(self, settings):
            super().__init__(settings, lambda s: FakeRuntime(s, events))

    monkeypatch.setattr(resources, "RoutedProvider", InjectedRouter)
    monkeypatch.setattr(
        resources,
        "embedding_provider",
        lambda _: pytest.fail("General chat must not load embeddings"),
    )
    args = cli.parser().parse_args(
        ["chat", "Hello", "--mode", "general", "--model", "large", "--debug", "--json"]
    )
    result, code = await cli.execute(args, save(catalog))
    assert code == 0 and result["answer"] == "large"
    assert result["routing"]["selected_model"] == "large"
    assert not (catalog[0].data_dir / "grimoire.sqlite3").exists()


async def test_unavailable_unloaded_daemon_can_fall_back(catalog):
    events = []

    class UnavailableRuntime(FakeRuntime):
        async def unload(self):
            pytest.fail("A runtime which never became ready has nothing to unload")

    def factory(settings):
        if settings.native_model_path.stem == "large":
            return UnavailableRuntime(settings, events, fail=True)
        return FakeRuntime(settings, events)

    router = RoutedProvider(save(catalog), factory)
    try:
        with routing_scope("general", "large"):
            assert await router.chat([]) == "small"
        assert router.last_route["fallback_used"]
        assert events[:3] == [("load", "large"), ("close", "large"), ("load", "small")]
    finally:
        await router.close()
