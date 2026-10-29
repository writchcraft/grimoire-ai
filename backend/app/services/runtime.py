"""Application resources shared by HTTP and local command workflows."""

import hashlib
import os
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

from starlette.concurrency import run_in_threadpool

from app.ai.registry import embedding_provider, generation_provider
from app.ai.routing import RoutedProvider
from app.services.storage import Storage


def embedding_identity(settings):
    if settings.embedding_provider == "ollama":
        return settings.embedding_model
    path = settings.native_embedding_model_path
    identity = f"native:{settings.embedding_model}:{path}"
    if path is None:
        return identity
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())

        def metadata(s):
            return (s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino)

        if metadata(before) != metadata(after) or metadata(after) != metadata(path.stat()):
            raise RuntimeError("Embedding weights changed during identity inspection; restart.")
        # Identity survives host/path changes; full hashing detects every changed byte.
        return f"native:{settings.embedding_model}:sha256:{digest.hexdigest()}"
    except OSError:
        # Diagnostics must still report missing/unreadable weights. This identity also
        # prevents an existing populated index from being opened without its weights.
        return f"{identity}:unavailable"


@asynccontextmanager
async def application_runtime(
    settings, provider=None, embeddings=None, *, generation=True, embedding=True, storage=True
):
    async with AsyncExitStack() as stack:
        state = SimpleNamespace(settings=settings)
        if storage:
            state.storage = await run_in_threadpool(
                Storage, settings.data_dir, embedding_identity(settings)
            )
            stack.push_async_callback(run_in_threadpool, state.storage.close)
        for name, supplied, factory, enabled in (
            ("provider", provider, generation_provider, generation),
            ("embeddings", embeddings, embedding_provider, embedding),
        ):
            if enabled:
                runtime = supplied or (
                    RoutedProvider(settings) if name == "provider" else factory(settings)
                )
                setattr(state, name, runtime)
                stack.push_async_callback(runtime.close)
                await runtime.start()
        yield state


async def wait_ready(runtime):
    if isinstance(runtime, RoutedProvider):
        return await runtime.ensure_ready()
    # Native startup deliberately schedules background loading for the web app.
    if getattr(runtime, "loading", None) is not None:
        await runtime.loading
    health = await runtime.health()
    if health["state"] != "ready":
        from app.core.errors import ProviderError

        raise ProviderError(health.get("reason") or "Configured model is unavailable.")
    return health


async def application_status(state):
    dependency = await state.provider.health()
    embeddings = await state.embeddings.health()
    docs = await run_in_threadpool(state.storage.documents)
    return {
        "status": "ready" if dependency["state"] == embeddings["state"] == "ready" else "degraded",
        "ai": dependency,
        "generation_models": [
            {"id": m["id"], "display_name": m["display_name"], "readiness": m["state"]}
            for m in state.provider.inventory()
        ]
        if isinstance(state.provider, RoutedProvider)
        else [],
        "embeddings": embeddings,
        "model": dependency["model"],
        "embedding_model": state.settings.embedding_model,
        "documents": len(docs),
        "chunks": sum(d["chunk_count"] for d in docs),
        "upload_limit_bytes": state.settings.max_upload_bytes,
    }
