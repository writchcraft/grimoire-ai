import asyncio
import json
import sqlite3
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.errors import DocumentError, ProviderError
from app.ingestion.pipeline import ingest
from app.models import (
    ChatRequest,
    ChatResponse,
    IncidentRequest,
    IngestRequest,
    LogRequest,
    ServiceInput,
)
from app.rag.retrieval import NO_EVIDENCE, prepare_chat
from app.services.log_analysis import analyse, incident_markdown

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Process liveness; dependency readiness is separate."""
    return {"status": "ok", "version": "0.1.0"}


@router.get("/api/status")
async def status(request: Request) -> dict:
    state = request.app.state
    dependency = await state.provider.health()
    embedding_status = await state.embeddings.health()
    docs = await run_in_threadpool(state.storage.documents)
    return {
        "status": "ready"
        if dependency["state"] == embedding_status["state"] == "ready"
        else "degraded",
        "ai": dependency,
        "embeddings": embedding_status,
        "model": dependency["model"],
        "embedding_model": state.settings.embedding_model,
        "documents": len(docs),
        "chunks": sum(d["chunk_count"] for d in docs),
        "upload_limit_bytes": state.settings.max_upload_bytes,
    }


@router.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    state = request.app.state
    # Streaming holds the semaphore for the generator's lifetime.
    if body.stream:

        async def events():
            def event(kind: str, **values) -> str:
                return json.dumps({"type": kind, **values}) + "\n"

            async with state.inference_slots:
                try:
                    messages, sources = await prepare_chat(
                        body, state.storage, state.embeddings, state.settings
                    )
                    yield event("sources", sources=[s.model_dump() for s in sources])
                    if not messages:
                        yield event("token", content=NO_EVIDENCE)
                    else:
                        async with aclosing(state.provider.stream(messages)) as tokens:
                            async for token in tokens:
                                if await request.is_disconnected():
                                    return
                                yield event("token", content=token)
                    yield event("done", grounded=bool(sources))
                except (ProviderError, DocumentError) as exc:
                    yield event("error", detail=str(exc))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield event(
                        "error", detail="Request failed; retry or inspect local application logs."
                    )

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
    async with state.inference_slots:
        messages, sources = await prepare_chat(
            body, state.storage, state.embeddings, state.settings
        )
        answer = await state.provider.chat(messages) if messages else NO_EVIDENCE
        return ChatResponse(answer=answer, sources=sources, grounded=bool(sources))


@router.post("/api/documents/upload", status_code=201)
async def upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    service: str | None = Form(default=None, max_length=100),
) -> dict:
    state = request.app.state
    try:
        raw = await file.read(state.settings.max_upload_bytes + 1)
        async with state.ingestion_slot:
            return await ingest(
                state.storage,
                state.embeddings,
                state.settings,
                file.filename or "",
                raw,
                service=service,
            )
    finally:
        await file.close()


@router.post("/api/documents/ingest", status_code=201)
async def ingest_text(body: IngestRequest, request: Request) -> dict:
    """Ingest supplied text; source_path is a label, never a server filesystem path to read."""
    state = request.app.state
    async with state.ingestion_slot:
        return await ingest(
            state.storage,
            state.embeddings,
            state.settings,
            body.filename,
            body.content.encode(),
            body.source_path,
            body.service,
        )


@router.get("/api/documents")
def documents(request: Request) -> list[dict]:
    return request.app.state.storage.documents()


@router.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: UUID, request: Request) -> None:
    if not request.app.state.storage.delete_document(str(document_id)):
        raise HTTPException(404, "Document not found")


@router.get("/api/search")
def search(
    request: Request,
    q: str = Query(min_length=1, max_length=500),
    service: str | None = Query(default=None, max_length=100),
    document_type: str | None = Query(default=None, max_length=10),
    source: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=8, ge=1, le=30),
) -> dict:
    store = request.app.state.storage
    return {
        "chunks": store.lexical_search(q, limit, service, document_type, source),
        "services": store.services(q),
    }


@router.post("/api/logs/analyse")
async def logs(body: LogRequest, request: Request) -> dict:
    async with request.app.state.inference_slots:
        return await analyse(
            body.content, body.service, body.question, body.use_model, request.app.state.provider
        )


@router.post("/api/incidents/summarise")
async def incident(body: IncidentRequest, request: Request) -> dict:
    async with request.app.state.inference_slots:
        result = await analyse(
            body.content,
            body.service,
            body.question,
            body.use_model,
            request.app.state.provider,
            notes=body.notes,
        )
    return {
        "markdown": incident_markdown(
            body.title, body.service, body.notes, result, result["explanation"]
        ),
        "analysis": result,
    }


@router.get("/api/services")
def services(request: Request, q: str = Query(default="", max_length=500)) -> list[dict]:
    return request.app.state.storage.services(q)


@router.post("/api/services", status_code=201)
def save_service(body: ServiceInput, request: Request) -> dict:
    try:
        return request.app.state.storage.save_service(body)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A service with that name already exists") from exc


@router.delete("/api/services/{service_id}", status_code=204)
def delete_service(service_id: UUID, request: Request) -> None:
    if not request.app.state.storage.delete_service(str(service_id)):
        raise HTTPException(404, "Service not found")
