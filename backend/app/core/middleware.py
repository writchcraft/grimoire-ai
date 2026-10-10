import logging
import time
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.responses import JSONResponse


class RequestGuard:
    """Bound bodies even without Content-Length; reject cross-origin writes."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id, start = uuid4().hex, time.monotonic()
        headers = dict(scope["headers"])
        status = 500

        async def send_response(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; "
                            b"style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                            b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                            b"form-action 'self'",
                        ),
                    ]
                )
            await send(message)

        async def reject(code: int, detail: str):
            await JSONResponse({"detail": detail, "request_id": request_id}, code)(
                scope, receive, send_response
            )

        try:
            if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
                origin = headers.get(b"origin")
                if origin:
                    try:
                        host = urlsplit(origin.decode()).netloc
                    except (ValueError, UnicodeError):
                        host = ""
                    if not host or host.encode() != headers.get(b"host", b""):
                        await reject(403, "Cross-origin writes are disabled")
                        return
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_bytes:
                        await reject(413, "Request body exceeds the configured upload limit")
                        return
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay, send_response)
            else:
                await self.app(scope, receive, send_response)
        finally:
            route = scope.get("route")
            logging.getLogger("grimoire.requests").info(
                "request_complete",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "path": getattr(route, "path", "unmatched"),
                    "status": status,
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                },
            )
