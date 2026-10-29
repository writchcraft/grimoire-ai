import asyncio
import json
import sqlite3
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.ai.routing import route_decision, routing_scope
from app.api.events import router as events_router
from app.api.host import router as host_router
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
from app.services.grounding import grounded_answer
from app.services.live import live_answer, probe_and_record, probe_many
from app.services.log_analysis import analyse, incident_markdown
from app.services.runtime import application_status

router = APIRouter()
router.include_router(events_router)
router.include_router(host_router)


def check_model(provider, task, model):
    if hasattr(provider, "registry"):
        try:
            provider.registry.candidates(task, model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.get("/health")
async def health() -> dict:
    """Process liveness; dependency readiness is separate."""
    return {"status": "ok", "version": "0.1.0"}


@router.get("/api/status")
async def status(request: Request) -> dict:
    return await application_status(request.app.state)


@router.get("/ready")
async def ready(request: Request):
    """Dependency probes only: no model loading, inference or index scan."""
    state = request.app.state
    try:
        storage = await run_in_threadpool(state.storage.readiness)
    except (OSError, sqlite3.Error):
        storage = {"state": "failed"}
    generation = await state.provider.health()
    embedding = await state.embeddings.health()
    checks = {"generation": generation, "embedding": embedding, "storage": storage}
    usable = all(item["state"] == "ready" for item in checks.values())
    return JSONResponse(
        {"status": "ready" if usable else "degraded", **checks}, status_code=200 if usable else 503
    )


async def live_branch(body: ChatRequest, state) -> dict | None:
    """Questions about current service state are answered from observations, not documents."""
    if body.mode != "knowledge":
        return None
    from app.services.windows_events import event_chat, event_question

    if event_question(body.message):
        with routing_scope("logs", body.model):
            async with state.inference_slots:
                return await event_chat(body, state.storage, state.provider)
    if not body.explain:
        return await live_answer(body, state.storage)
    check_model(state.provider, "summarisation", body.model)
    with routing_scope("summarisation", body.model):
        async with state.inference_slots:
            return await live_answer(body, state.storage, state.provider)


@router.post("/api/chat", response_model=ChatResponse, response_model_exclude_none=True)
async def chat(body: ChatRequest, request: Request):
    state = request.app.state
    async with state.probe_slots:
        try:
            live = await live_branch(body, state)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if live is not None:
        if not body.stream:
            return ChatResponse(**live)

        async def live_events():
            def event(kind: str, **values) -> str:
                return json.dumps({"type": kind, **values}) + "\n"

            yield event("sources", sources=[])
            yield event("live", live=[item.model_dump() for item in live["live"]])
            yield event("token", content=live["answer"])
            if live["interpretation"]:
                yield event("interpretation", content=live["interpretation"])
            yield event("done", grounded=live["grounded"], quality=live["quality"], claims=[])

        return StreamingResponse(
            live_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    check_model(state.provider, "rag" if body.mode == "knowledge" else body.task, body.model)
    # Streaming holds the semaphore for the generator's lifetime.
    if body.stream:

        async def events():
            def event(kind: str, **values) -> str:
                return json.dumps({"type": kind, **values}) + "\n"

            with routing_scope("rag" if body.mode == "knowledge" else body.task, body.model):
                async with state.inference_slots:
                    try:
                        messages, sources = await prepare_chat(
                            body, state.storage, state.embeddings, state.settings
                        )
                        if body.mode == "knowledge":
                            result = (
                                await grounded_answer(
                                    state.provider, messages, sources, body.message
                                )
                                if messages
                                else {
                                    "answer": NO_EVIDENCE,
                                    "sources": [],
                                    "grounded": False,
                                    "quality": "insufficient_evidence",
                                    "claims": [],
                                }
                            )
                            yield event(
                                "sources", sources=[s.model_dump() for s in result["sources"]]
                            )
                            # Buffer knowledge output until validation; no unchecked tokens escape.
                            yield event("token", content=result["answer"])
                            yield event(
                                "done",
                                grounded=result["grounded"],
                                quality=result["quality"],
                                claims=result["claims"],
                                **({"routing": route_decision.get()} if body.debug else {}),
                            )
                            return
                        yield event("sources", sources=[])
                        if not messages:
                            yield event("token", content=NO_EVIDENCE)
                        else:
                            async with aclosing(state.provider.stream(messages)) as tokens:
                                async for token in tokens:
                                    if await request.is_disconnected():
                                        return
                                    yield event("token", content=token)
                        yield event(
                            "done",
                            grounded=bool(sources),
                            **({"routing": route_decision.get()} if body.debug else {}),
                        )
                    except (ProviderError, DocumentError) as exc:
                        yield event("error", detail=str(exc))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        yield event(
                            "error",
                            detail="Request failed; retry or inspect local application logs.",
                        )

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
    with routing_scope("rag" if body.mode == "knowledge" else body.task, body.model):
        async with state.inference_slots:
            messages, sources = await prepare_chat(
                body, state.storage, state.embeddings, state.settings
            )
            if body.mode == "knowledge" and messages:
                return ChatResponse(
                    **await grounded_answer(state.provider, messages, sources, body.message),
                    routing=route_decision.get() if body.debug else None,
                )
            answer = await state.provider.chat(messages) if messages else NO_EVIDENCE
            return ChatResponse(
                routing=route_decision.get() if body.debug else None,
                answer=answer,
                sources=sources,
                grounded=bool(sources),
                quality="insufficient_evidence" if body.mode == "knowledge" else "unvalidated",
            )


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
    check_model(request.app.state.provider, "logs", body.model)
    with routing_scope("logs", body.model):
        async with request.app.state.inference_slots:
            result = await analyse(
                body.content,
                body.service,
                body.question,
                body.use_model,
                request.app.state.provider,
            )
            if body.debug:
                result["routing"] = route_decision.get()
            return result


@router.post("/api/incidents/summarise")
async def incident(body: IncidentRequest, request: Request) -> dict:
    check_model(request.app.state.provider, "incidents", body.model)
    extra = None
    if body.event_selection:
        from app.api.events import selection
        from app.services.windows_events import assemble, correlate

        records = await run_in_threadpool(selection, body.event_selection)
        extra = assemble(records)
        extra["correlations"] = correlate(records, request.app.state.storage, body.service)
    with routing_scope("incidents", body.model):
        async with request.app.state.inference_slots:
            result = await analyse(
                body.content,
                body.service,
                body.question,
                body.use_model,
                request.app.state.provider,
                notes=body.notes,
                extra_evidence=extra,
            )
        if body.debug:
            result["routing"] = route_decision.get()
        record = {
            "markdown": incident_markdown(
                body.title, body.service, body.notes, result, result["explanation"]
            ),
            "analysis": result,
        }
        if body.include_host:
            from app.services.host import context

            host_context = await run_in_threadpool(
                context,
                request.app.state.settings,
                request.app.state.storage,
                body.service,
                records if body.event_selection else (),
            )
            result["host_context"] = host_context
            record["markdown"] += (
                "\n\nHost evidence (explicit point-in-time inspection):\n"
                + json.dumps(host_context, indent=2)
            )
        return await run_in_threadpool(request.app.state.storage.save_incident, record)


@router.get("/api/incidents")
def incidents(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    return request.app.state.storage.incidents(limit)


@router.delete("/api/incidents/{incident_id}", status_code=204)
def delete_incident(incident_id: UUID, request: Request) -> None:
    if not request.app.state.storage.delete_incident(str(incident_id)):
        raise HTTPException(404, "Incident not found")


@router.get("/api/services")
def services(
    request: Request, q: str = Query(default="", max_length=500), health: bool = False
) -> list[dict]:
    """Inventory; health=true attaches each service's latest stored observation (no probing)."""
    store = request.app.state.storage
    records = store.services(q)
    if health:
        from app.services.dns import summary

        latest = store.latest_observations()
        for record in records:
            record["health"] = latest.get(record["id"])
            record["dns"] = summary(store, record)
            from app.services.tls import summary as tls_summary

            record["tls"] = tls_summary(store, record)
    return records


def monitored_service(request: Request, service_id: UUID, require_monitor: bool = True) -> dict:
    service = request.app.state.storage.service(str(service_id))
    if service is None:
        raise HTTPException(404, "Service not found")
    if require_monitor and not service.get("monitor"):
        raise HTTPException(422, "Service has no monitor configured")
    return service


@router.post("/api/services/probe")
async def probe_all(request: Request) -> list[dict]:
    """Read-only probe of every monitored service; only stored monitor URLs are ever requested."""
    state = request.app.state
    services = await run_in_threadpool(state.storage.services)
    if sum(bool(s.get("monitor")) for s in services) > 50:
        raise HTTPException(422, "Batch limit is 50 services; probe individually")
    async with state.probe_slots:
        observed = await probe_many(state.storage, services)
    return list(observed.values())


@router.post("/api/services/{service_id}/probe")
async def probe_one(service_id: UUID, request: Request) -> dict:
    service = monitored_service(request, service_id)
    async with request.app.state.probe_slots:
        return await probe_and_record(request.app.state.storage, service)


@router.get("/api/services/{service_id}/health")
def service_health(
    service_id: UUID, request: Request, history: int = Query(default=10, ge=1, le=50)
) -> dict:
    service = monitored_service(request, service_id, require_monitor=False)
    recent = request.app.state.storage.observations(service["id"], history)
    return {
        "service": service["name"],
        "monitored": bool(service.get("monitor")),
        "state": recent[0]["state"] if recent else "unknown",
        "latest": recent[0] if recent else None,
        "history": recent,
    }


@router.post("/api/services/{service_id}/dns")
async def inspect_dns(service_id: UUID, request: Request) -> dict:
    from app.services.dns import inspect_and_record

    service = monitored_service(request, service_id, require_monitor=False)
    async with request.app.state.probe_slots:
        return await inspect_and_record(request.app.state.storage, service)


@router.get("/api/services/{service_id}/dns")
def service_dns(service_id: UUID, request: Request) -> dict:
    from app.services.dns import summary

    service = monitored_service(request, service_id, require_monitor=False)
    return summary(request.app.state.storage, service)


@router.post("/api/services/{service_id}/tls")
async def inspect_tls(service_id: UUID, request: Request) -> dict:
    from app.services.tls import inspect_and_record

    service = monitored_service(request, service_id, require_monitor=False)
    async with request.app.state.probe_slots:
        return await inspect_and_record(request.app.state.storage, service)


@router.get("/api/services/{service_id}/tls")
def service_tls(service_id: UUID, request: Request) -> dict:
    from app.services.tls import summary

    service = monitored_service(request, service_id, require_monitor=False)
    return summary(request.app.state.storage, service)


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
