"""Local read-only host inspections. No inventory is ever passed to a model."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from app.services import host

router = APIRouter(prefix="/api/host", tags=["Host"])


@router.get("")
def overview(request: Request):
    return host.inspect_and_retain(request.app.state.settings)


@router.get("/history")
def history(request: Request, limit: int = Query(default=20, ge=1, le=50)):
    return host.history(request.app.state.settings.data_dir, limit)


@router.get("/correlations")
def correlations(request: Request, service: str | None = Query(default=None, max_length=100)):
    from app.services.windows_events import EventQuery, read_events

    events = read_events(EventQuery(channel="System", level="all", limit=100))
    result = host.context(
        request.app.state.settings, request.app.state.storage, service, events["records"]
    )
    result["event_scope"] = {k: v for k, v in events.items() if k != "records"}
    return result


@router.get("/services")
def services(request: Request):
    try:
        query = host.ServiceQuery.model_validate(dict(request.query_params))
    except ValidationError:
        raise HTTPException(422, "Invalid bounded service filters") from None
    return host.inspect_and_retain(request.app.state.settings, "services", query)


@router.get("/services/{name}")
def service(name: str, request: Request):
    try:
        query = host.ServiceQuery(name=name)
    except ValidationError:
        raise HTTPException(422, "Invalid service name") from None
    page = host.inspect_and_retain(request.app.state.settings, "services", query, True)
    if page["status"] == "available" and not page["records"]:
        raise HTTPException(404, "Windows service not found")
    return page


@router.get("/{section}")
def section(section: str, request: Request):
    if section not in {"system", "resources", "disks", "network"}:
        raise HTTPException(404, "Unknown host section")
    return host.inspect_and_retain(request.app.state.settings, section)
