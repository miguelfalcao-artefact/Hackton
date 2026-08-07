"""Per-request context, so log lines and events can be correlated without
threading a dozen arguments through every function.

The middleware fills this in; routers read it. ContextVar keeps it correct
under concurrency.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RequestContext:
    request_id: str = ""
    api_key_id: str | None = None
    tier: str | None = None
    # Routers drop identifiers here as they learn them; the access log picks
    # them up at the end of the request.
    extra: dict = field(default_factory=dict)


_ctx: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def set_context(ctx: RequestContext) -> None:
    _ctx.set(ctx)


def get_context() -> RequestContext:
    ctx = _ctx.get()
    if ctx is None:
        ctx = RequestContext()
        _ctx.set(ctx)
    return ctx


def annotate(**fields) -> None:
    """Attach fields to the current request's access-log line."""
    get_context().extra.update({k: v for k, v in fields.items() if v is not None})
