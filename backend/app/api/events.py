"""Bounded local event reads and selected evidence analysis."""

import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.ai.routing import routing_scope
from app.models import EventReference, InputModel
from app.services import windows_events as events
from app.services.log_analysis import incident_markdown

router = APIRouter(prefix="/api/events", tags=["Windows events"])


class EventAnalysis(InputModel):
    query: events.EventQuery = Field(default_factory=events.EventQuery)
    selected: list[EventReference] = Field(default_factory=list, max_length=50)
    question: str = Field(default="Explain these Windows events.", max_length=2000)
    notes: str = Field(default="", max_length=8000)
    service: str = Field(default="unknown", max_length=100)
    use_model: bool = False
    incident_title: str | None = Field(default=None, min_length=1, max_length=150)


def selection(refs):
    records = []
    seen = set()
    total = 0
    deadline = time.monotonic() + 10
    for ref in refs:
        if ref.evidence_id in seen:
            continue
        seen.add(ref.evidence_id)
        if time.monotonic() >= deadline:
            raise HTTPException(503, "Selection read deadline reached; select fewer events")
        result = events.read_events(
            events.EventQuery(channel=ref.channel, level="all", record_id=ref.record_id, limit=1)
        )
        if not result["records"] or result["records"][0]["evidence_id"] != ref.evidence_id:
            raise HTTPException(
                409, "Selected event no longer available or source identity changed"
            )
        record = result["records"][0]
        total += len(json.dumps(record).encode("utf-8"))
        if total > 1_048_576:
            raise HTTPException(413, "Selected event data exceeds one MiB; select fewer events")
        records.append(record)
    return records


@router.get("/channels")
def channels():
    return events.channels()


@router.get("")
def recent(request: Request):
    try:
        query = events.EventQuery.model_validate(dict(request.query_params))
    except (ValueError, ValidationError):
        raise HTTPException(
            422, "Invalid bounded event filters; timestamps require timezone"
        ) from None
    return events.read_events(query)


@router.get("/{record_id}")
def inspect(record_id: int, channel: Literal["System", "Application", "Security"] = "System"):
    if not 1 <= record_id <= 2**63 - 1:
        raise HTTPException(422, "Invalid record ID")
    result = events.read_events(
        events.EventQuery(channel=channel, record_id=record_id, level="all", limit=1)
    )
    if result["status"] == "available" and not result["records"]:
        raise HTTPException(404, "Event record not found")
    return result


@router.post("/analyse")
async def analyse(body: EventAnalysis, request: Request):
    if body.selected:
        records = await run_in_threadpool(selection, body.selected)
        source = {"status": "selected", "issues": []}
    else:
        source = await run_in_threadpool(events.read_events, body.query)
        records = source["records"]
    with routing_scope("logs"):
        async with request.app.state.inference_slots:
            result = await events.analyse_events(
                records,
                body.question,
                request.app.state.provider,
                body.use_model,
                body.notes,
                body.service,
                request.app.state.storage,
            )
    result["retrieval"] = {k: v for k, v in source.items() if k != "records"}
    if body.incident_title:
        record = {
            "analysis": result,
            "markdown": incident_markdown(
                body.incident_title, body.service, body.notes, result, result["explanation"]
            ),
        }
        return await run_in_threadpool(request.app.state.storage.save_incident, record)
    return result
