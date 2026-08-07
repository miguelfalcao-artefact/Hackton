"""Request-scoped plumbing: correlation id, timing, and the access log line.

This middleware is the primary producer of `/var/log/app/api.jsonl`.
"""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .context import RequestContext, set_context
from .logging_setup import log

logger = logging.getLogger("access")

REQUEST_ID_HEADER = "X-Request-Id"

# Health probes fire every few seconds and would drown the interesting traffic.
_QUIET_PATHS = {"/health", "/ready", "/favicon.ico"}


def _route_template(request: Request) -> str:
    """`/v1/orders/{order_id}` rather than the concrete id.

    Without this, grouping the log by endpoint is impossible — every order id
    would look like its own route.
    """
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        ctx = RequestContext(request_id=request_id)
        set_context(ctx)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Nothing downstream turned this into a response, so it is a genuine
            # 500. Log it with the traceback, then let Starlette finish the job.
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "http_request",
                extra={
                    "event": "http_request",
                    "fields": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "route": _route_template(request),
                        "status": 500,
                        "latency_ms": round(elapsed_ms, 2),
                        "api_key_id": ctx.api_key_id,
                        "tier": ctx.tier,
                        **ctx.extra,
                    },
                },
            )
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        if request.url.path not in _QUIET_PATHS:
            level = "error" if response.status_code >= 500 else (
                "warning" if response.status_code >= 400 else "info"
            )
            # Merged into a dict rather than splatted alongside keyword
            # arguments: a router or error handler is free to annotate a field
            # that happens to share a name here, and the annotation just wins
            # instead of raising "multiple values for keyword argument".
            fields = {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "route": _route_template(request),
                "status": response.status_code,
                "latency_ms": round(elapsed_ms, 2),
                "api_key_id": ctx.api_key_id,
                "tier": ctx.tier,
                "user_agent": request.headers.get("user-agent"),
                "client_ip": request.client.host if request.client else None,
                "query": str(request.url.query) or None,
                **ctx.extra,
            }
            log(logger, level, "http_request", **fields)

        return response
