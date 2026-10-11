import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai.embeddings import OllamaEmbeddings
from app.ai.native import NativeEmbeddings, NativeProvider
from app.ai.ollama import OllamaProvider
from app.ai.registry import embedding_provider, generation_provider
from app.core.config import Settings
from app.core.errors import ProviderError
from app.main import create_app


class FakeLlama:
    def __init__(self, **options):
        self.options = options
        self.thread = threading.get_ident()
        self.closed = False
        self.stream_closed = False

    def create_chat_completion(self, **options):
        assert threading.get_ident() == self.thread
        if not options["stream"]:
            return {"choices": [{"message": {"content": "Local answer"}}]}

        def chunks():
            try:
                for token in ["Local ", "answer"]:
                    assert threading.get_ident() == self.thread
                    yield {"choices": [{"delta": {"content": token}, "finish_reason": None}]}
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            finally:
                self.stream_closed = True

        return chunks()

    def embed(self, texts, **options):
        assert threading.get_ident() == self.thread
        assert options == {"normalize": True, "truncate": False}
        return [[1.0, 0.0] for _ in texts]

    def close(self):
        assert threading.get_ident() == self.thread
        self.closed = True


@pytest.fixture
def native_settings(settings, tmp_path):
    path = tmp_path / "test.gguf"
    path.write_bytes(b"fake; not an actual GGUF")
    return settings.model_copy(
        update={
            "native_model_path": path,
            "native_embedding_model_path": path,
            "native_threads": 2,
            "native_gpu_layers": 0,
        }
    )


async def load(runtime):
    await runtime.start()
    await runtime.loading
    return runtime


async def test_provider_selection(settings):
    for provider, expected, embedding_expected in [
        ("native", NativeProvider, NativeEmbeddings),
        ("ollama", OllamaProvider, OllamaEmbeddings),
    ]:
        config = settings.model_copy(
            update={"ai_provider": provider, "embedding_provider": provider}
        )
        generation, embeddings = generation_provider(config), embedding_provider(config)
        assert isinstance(generation, expected)
        assert isinstance(embeddings, embedding_expected)
        await generation.close()
        await embeddings.close()


@pytest.mark.parametrize("field", ["ai_provider", "embedding_provider"])
def test_invalid_provider_fails_clearly(field):
    with pytest.raises(ValidationError, match=field):
        Settings(**{field: "cloud"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("native_context_size", 0),
        ("native_threads", 0),
        ("native_gpu_layers", -2),
    ],
)
def test_invalid_native_options(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


async def test_missing_and_unconfigured_models(settings, tmp_path):
    for path, state in [(None, "not_configured"), (tmp_path / "missing.gguf", "model_missing")]:
        runtime = await load(
            NativeProvider(settings.model_copy(update={"native_model_path": path}))
        )
        try:
            status = await runtime.health()
            assert status["state"] == state and status["reason"]
            with pytest.raises(ProviderError):
                await runtime.chat([])
        finally:
            await runtime.close()


async def test_load_once_chat_stream_metadata_and_close(native_settings):
    models = []

    def loader(**options):
        model = FakeLlama(**options)
        models.append(model)
        return model

    runtime = await load(NativeProvider(native_settings, loader=loader))
    try:
        await runtime.start()
        status = await runtime.health()
        assert status["state"] == "ready"
        assert status["model"] == "test.gguf" and status["context_size"] == 8192
        assert models[0].options["n_threads"] == 2
        assert models[0].options["n_gpu_layers"] == 0
        assert await runtime.chat([]) == "Local answer"
        assert "".join([token async for token in runtime.stream([])]) == "Local answer"
        assert len(models) == 1 and models[0].stream_closed
    finally:
        await runtime.close()
    assert models[0].closed


async def test_load_failure_redacted(native_settings):
    def broken(**options):
        raise RuntimeError("secret path or vendor internals")

    runtime = await load(NativeProvider(native_settings, loader=broken))
    try:
        status = await runtime.health()
        assert status["state"] == "failed" and "secret" not in status["reason"]
        with pytest.raises(ProviderError, match="GGUF loading failed"):
            await runtime.chat([])
    finally:
        await runtime.close()


async def test_missing_binding_redacted(native_settings):
    def broken(**options):
        raise ImportError("secret")

    runtime = await load(NativeProvider(native_settings, loader=broken))
    try:
        assert (await runtime.health())["state"] == "failed"
        assert "native dependencies" in (await runtime.health())["reason"]
    finally:
        await runtime.close()


async def test_loading_and_health_do_not_block_loop(native_settings):
    def slow(**options):
        time.sleep(0.15)
        return FakeLlama(**options)

    runtime = NativeProvider(native_settings, loader=slow)
    try:
        await runtime.start()
        assert (await runtime.health())["state"] == "loading"
        await asyncio.sleep(0.01)
        assert not runtime.loading.done()
        await runtime.loading
        assert (await runtime.health())["state"] == "ready"
    finally:
        await runtime.close()


async def test_cancelled_stream_releases_runtime(native_settings):
    runtime = await load(NativeProvider(native_settings, loader=FakeLlama))
    try:
        stream = runtime.stream([])
        assert await anext(stream) == "Local "
        await stream.aclose()
        assert runtime.model.stream_closed
        assert await runtime.chat([]) == "Local answer"
    finally:
        await runtime.close()


async def test_cancelled_generation_drains_worker(native_settings):
    started = threading.Event()
    release = threading.Event()

    class Slow(FakeLlama):
        def create_chat_completion(self, **options):
            started.set()
            release.wait(timeout=2)
            return super().create_chat_completion(**options)

    runtime = await load(NativeProvider(native_settings, loader=Slow))
    task = asyncio.create_task(runtime.chat([]))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert runtime.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await runtime.chat([]) == "Local answer"
    finally:
        release.set()
        await runtime.close()


async def test_embedding_runtime_is_separate(native_settings):
    runtime = await load(NativeEmbeddings(native_settings, loader=FakeLlama))
    try:
        assert runtime.model.options["embedding"] is True
        assert runtime.model.options["n_ctx"] == native_settings.native_embedding_context_size
        assert await runtime.embed(["hello"]) == [[1, 0]]
    finally:
        await runtime.close()


@pytest.mark.parametrize("result", [[], [[0, 0]], [[float("nan")]], [[[1, 2]]], [["bad"]]])
async def test_invalid_native_embeddings_rejected(native_settings, result):
    class Invalid(FakeLlama):
        def embed(self, *args, **kwargs):
            return result

    runtime = await load(NativeEmbeddings(native_settings, loader=Invalid))
    try:
        with pytest.raises(ProviderError):
            await runtime.embed(["hello"])
    finally:
        await runtime.close()


def test_native_adapter_all_api_features_with_fake_binding(native_settings):
    generation = NativeProvider(native_settings, loader=FakeLlama)
    embeddings = NativeEmbeddings(native_settings, loader=FakeLlama)
    with TestClient(
        create_app(native_settings, generation, embeddings), base_url="http://localhost"
    ) as client:
        response = client.post("/api/chat", json={"message": "hello", "mode": "general"})
        assert response.status_code == 200 and response.json()["answer"] == "Local answer"
        response = client.post(
            "/api/documents/ingest",
            json={
                "filename": "grafana.md",
                "content": "Grafana port 3000",
            },
        )
        assert response.status_code == 201
        status = client.get("/api/status").json()
        assert status["status"] == "ready" and status["ai"]["backend"] == "llama.cpp"
        assert "ollama" not in status
        response = client.post("/api/chat", json={"message": "Grafana port?", "stream": True})
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["sources"] and events[-1] == {"type": "done", "grounded": True}
        assert "".join(e.get("content", "") for e in events) == "Local answer"
        body = {"content": "ERROR connection refused", "service": "grafana", "use_model": True}
        assert client.post("/api/logs/analyse", json=body).json()["explanation"] == "Local answer"
        report = client.post("/api/incidents/summarise", json={**body, "title": "Test"})
        assert report.status_code == 200 and "Local answer" in report.json()["markdown"]
        model = generation.model
    assert model.closed


@pytest.mark.parametrize("stream", [False, True])
async def test_native_inference_failures_are_redacted_and_recoverable(native_settings, stream):
    class Broken(FakeLlama):
        def create_chat_completion(self, **options):
            raise RuntimeError("secret native internals")

    runtime = await load(NativeProvider(native_settings, loader=Broken))
    try:
        with pytest.raises(ProviderError) as error:
            if stream:
                async for _ in runtime.stream([]):
                    pass
            else:
                await runtime.chat([])
        assert "secret" not in str(error.value)
        assert (await runtime.health())["state"] == "ready"
        assert not runtime.lock.locked()
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [{"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}],
        [{"choices": [{"delta": {}, "finish_reason": "stop"}]}],
    ],
)
async def test_native_empty_and_incomplete_streams_fail(native_settings, chunks):
    class Incomplete(FakeLlama):
        def create_chat_completion(self, **options):
            return iter(chunks)

    runtime = await load(NativeProvider(native_settings, loader=Incomplete))
    try:
        with pytest.raises(ProviderError):
            async for _ in runtime.stream([]):
                pass
    finally:
        await runtime.close()


def test_start_without_ollama_or_models(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert client.get("/health").status_code == 200
        status = client.get("/api/status").json()
        assert status["status"] == "degraded" and status["ai"]["provider"] == "native"
        assert status["ai"]["state"] in {"loading", "not_configured"}
        response = client.post("/api/chat", json={"message": "hello", "mode": "general"})
        assert response.status_code == 503
        assert (
            client.post(
                "/api/logs/analyse",
                json={
                    "content": "ERROR failed",
                    "service": "test",
                    "use_model": False,
                },
            ).status_code
            == 200
        )
