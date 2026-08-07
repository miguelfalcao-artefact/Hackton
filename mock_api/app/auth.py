"""API-key authentication and the per-key rate limit.

Keys are static and come from the API_KEYS env var. Each one carries a tier,
and the tier sets the quota — so a single noisy consumer is visible in the logs
as one `api_key_id` producing all the 429s.
"""
from dataclasses import dataclass
from time import monotonic

from fastapi import Request

from .config import settings
from .errors import Forbidden, RateLimited, Unauthorized

API_KEY_HEADER = "X-API-Key"

_TIER_LIMITS = {
    "free": settings.rate_limit_free_per_min,
    "pro": settings.rate_limit_pro_per_min,
    "enterprise": settings.rate_limit_enterprise_per_min,
}


@dataclass(frozen=True)
class ApiKey:
    id: str
    key: str
    tier: str

    @property
    def limit_per_min(self) -> int:
        return _TIER_LIMITS.get(self.tier, settings.rate_limit_free_per_min)


def _parse_keys(raw: str) -> dict[str, ApiKey]:
    keys: dict[str, ApiKey] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(f"malformed API_KEYS entry: {entry!r} (want id:key:tier)")
        key_id, key, tier = (p.strip() for p in parts)
        keys[key] = ApiKey(id=key_id, key=key, tier=tier)
    return keys


API_KEYS: dict[str, ApiKey] = _parse_keys(settings.api_keys)


def resolve_api_key(request: Request) -> ApiKey:
    raw = request.headers.get(API_KEY_HEADER)
    if not raw:
        raise Unauthorized(f"missing {API_KEY_HEADER} header")
    api_key = API_KEYS.get(raw.strip())
    if api_key is None:
        raise Forbidden("unknown API key")
    return api_key


class SlidingWindowLimiter:
    """Per-key sliding window over a 60s span.

    In-process state is enough here: the sandbox runs a single API process, and
    keeping Redis out keeps the compose file to two services.
    """

    WINDOW_S = 60.0

    def __init__(self):
        self._hits: dict[str, list[float]] = {}

    def check(self, api_key: ApiKey) -> None:
        now = monotonic()
        cutoff = now - self.WINDOW_S
        hits = [t for t in self._hits.get(api_key.id, ()) if t > cutoff]

        if len(hits) >= api_key.limit_per_min:
            self._hits[api_key.id] = hits
            retry_after = max(1, int(self.WINDOW_S - (now - hits[0])) + 1)
            raise RateLimited(
                f"rate limit of {api_key.limit_per_min}/min exceeded for tier {api_key.tier}",
                retry_after=retry_after,
                limit_per_min=api_key.limit_per_min,
                tier=api_key.tier,
            )

        hits.append(now)
        self._hits[api_key.id] = hits


limiter = SlidingWindowLimiter()
