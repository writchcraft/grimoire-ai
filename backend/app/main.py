import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.ai.base import EmbeddingProvider, ModelProvider
from app.api.routes import router
from app.core.config import Settings
from app.core.errors import DocumentError, ProviderError
from app.core.logging import configure_logging
from app.core.middleware import RequestGuard
from app.services.runtime import application_runtime


def create_app(
    settings: Settings | None = None,
    provider: ModelProvider | None = None,
    embeddings: EmbeddingProvider | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with application_runtime(settings, provider, embeddings) as state:
            app.state.settings = state.settings
            app.state.storage = state.storage
            app.state.provider = state.provider
            app.state.embeddings = state.embeddings
            app.state.inference_slots = asyncio.Semaphore(2)
            app.state.ingestion_slot = asyncio.Semaphore(1)
            app.state.probe_slots = asyncio.Semaphore(2)
            logging.getLogger("grimoire").info("application_started")
            yield
        logging.getLogger("grimoire").info("application_stopped")

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
