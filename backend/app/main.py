import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.ai.base import EmbeddingProvider, ModelProvider
from app.ai.registry import embedding_provider, generation_provider
from app.api.routes import router
from app.core.config import Settings
from app.core.errors import DocumentError, ProviderError
from app.core.logging import configure_logging
from app.core.middleware import RequestGuard
from app.services.storage import Storage


def create_app(
    settings: Settings | None = None,
    provider: ModelProvider | None = None,
    embeddings: EmbeddingProvider | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.storage = await run_in_threadpool(
            Storage,
            settings.data_dir,
            (
                settings.embedding_model
                if settings.embedding_provider == "ollama"
                else f"native:{settings.embedding_model}:{settings.native_embedding_model_path}"
            ),
        )
        app.state.provider = provider or generation_provider(settings)
        app.state.embeddings = embeddings or embedding_provider(settings)
        app.state.inference_slots = asyncio.Semaphore(2)
        app.state.ingestion_slot = asyncio.Semaphore(1)
        logging.getLogger("grimoire").info("application_started")
        try:
            for runtime in (app.state.provider, app.state.embeddings):
                await runtime.start()
            yield
        finally:
            try:
                await app.state.provider.close()
            finally:
                try:
                    await app.state.embeddings.close()
                finally:
                    await run_in_threadpool(app.state.storage.close)

    app = FastAPI(
        title="Grimoire AI",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(RequestGuard, max_bytes=settings.max_upload_bytes + 65_536)
    app.include_router(router)

    @app.exception_handler(DocumentError)
    async def invalid_document(request: Request, exc: DocumentError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ProviderError)
    async def unavailable_provider(request: Request, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logging.getLogger("grimoire").error("unhandled_application_error")
        return JSONResponse(
            {"detail": "Internal error; inspect local application logs."}, status_code=500
        )

    return app


app = create_app()
