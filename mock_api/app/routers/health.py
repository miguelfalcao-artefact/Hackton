"""Liveness and readiness. Unauthenticated on purpose — probes have no key."""
from fastapi import APIRouter, Response

from .. import __version__
from ..config import settings
from ..db import ping
from ..schemas import HealthOut, ReadyOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok", service=settings.service_name, version=__version__)


@router.get("/ready", response_model=ReadyOut)
def ready(response: Response) -> ReadyOut:
    db_ok = ping()
    if not db_ok:
        response.status_code = 503
    return ReadyOut(status="ok" if db_ok else "degraded", db="ok" if db_ok else "unreachable")
