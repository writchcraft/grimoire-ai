import httpx
import pytest

from app.ai.embeddings import OllamaEmbeddings
from app.ai.ollama import OllamaProvider
from app.core.errors import ProviderError


async def test_ollama_chat_and_embeddings_contract(settings):
    async def handler(request):
        import json

        payload = json.loads(request.content)
        if request.url.path == "/api/embed":
            assert payload["truncate"] is False and payload["input"] == ["hello"]
            return httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})
        assert payload["stream"] is False and payload["model"] == settings.model
        return httpx.Response(200, json={"message": {"content": "hello"}})

    provider = OllamaProvider(settings, httpx.MockTransport(handler))
    try:
        assert await provider.chat([{"role": "user", "content": "hello"}]) == "hello"
        embeddings = OllamaEmbeddings(settings, httpx.MockTransport(handler))
        try:
            assert await embeddings.embed(["hello"]) == [[1, 2]]
        finally:
            await embeddings.close()
    finally:
        await provider.close()


@pytest.mark.parametrize("status", [404, 500])
async def test_provider_http_errors_are_redacted(settings, status):
    provider = OllamaProvider(
        settings,
        httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret provider internals")
        ),
    )
    try:
        with pytest.raises(ProviderError) as error:
            await provider.chat([])
        assert "secret" not in str(error.value)
        assert not (await provider.health())["reachable"]
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"embeddings": []},
        {"embeddings": [[0, 0]]},
        {"embeddings": [["bad"]]},
        {"embeddings": [[float("inf")]]},
        {"error": "missing model"},
    ],
)
async def test_invalid_embeddings_do_not_enter_index(settings, payload):
    # Non-finite JSON is invalid at the transport layer too; use a literal response body.
    import json

    provider = OllamaEmbeddings(
        settings,
        httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(payload))),
    )
    try:
        with pytest.raises(ProviderError):
            await provider.embed(["test"])
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "data",
    [
        b'{"message":{"content":"hello"}}\n',
        b'{"error":"secret internals"}\n',
        b"not json\n",
        b'{"done":true,"message":{"content":""}}\n',
    ],
)
async def test_incomplete_or_invalid_stream_fails(settings, data):
    provider = OllamaProvider(
        settings, httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    )
    try:
        with pytest.raises(ProviderError):
            async for _ in provider.stream([]):
                pass
    finally:
        await provider.close()


async def test_health_checks_required_models(settings):
    provider = OllamaProvider(
        settings,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {"name": settings.model},
                        {"name": settings.embedding_model + ":latest"},
                    ]
                },
            )
        ),
    )
    try:
        assert (await provider.health())["state"] == "ready"
    finally:
        await provider.close()
