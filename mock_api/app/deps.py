"""Shared FastAPI dependencies."""
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from . import events
from .auth import ApiKey, limiter, resolve_api_key
from .context import get_context
from .db import get_db
from .errors import RateLimited


def require_api_key(request: Request, db: Session = Depends(get_db)) -> ApiKey:
    """Authenticate, then charge the request against the key's quota.

    Declared as a dependency rather than middleware so the key requirement
    shows up in the OpenAPI docs the traffic generator is written against.
    """
    api_key = resolve_api_key(request)

    ctx = get_context()
    ctx.api_key_id = api_key.id
    ctx.tier = api_key.tier

    try:
        limiter.check(api_key)
    except RateLimited as exc:
        # A throttled request is a business event in its own right — it is how
        # a noisy consumer becomes visible in `domain_events`.
        events.emit(
            db,
            events.RATELIMIT_EXCEEDED,
            aggregate_type="api_key",
            aggregate_id=api_key.id,
            tier=api_key.tier,
            limit_per_min=api_key.limit_per_min,
            path=request.url.path,
        )
        db.commit()
        raise exc

    return api_key
