"""Application wiring: lifespan bootstrap, error shape, middleware, routes."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .config import settings
from .context import annotate, get_context
from .db import SessionLocal, create_all, wait_for_db
from .errors import ApiError, RateLimited
from .logging_setup import log, setup_logging
from .middleware import RequestLogMiddleware
from .routers import customers, health, orders, products
from .schemas import ErrorBody, ErrorResponse
from .seed import is_empty, seed

logger = setup_logging()
boot = logging.getLogger("boot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log(boot, "info", "startup_begin", version=__version__, log_dir=settings.log_dir)

    wait_for_db()
    create_all(drop_first=settings.reset_on_start)
    if settings.reset_on_start:
        log(boot, "warning", "database_reset", reason="RESET_ON_START=true")

    if settings.seed_on_start:
        with SessionLocal() as db:
            if is_empty(db):
                counts = seed(db)
                log(boot, "info", "seed_complete", **counts)
            else:
                log(boot, "info", "seed_skipped", reason="database already populated")

    log(boot, "info", "startup_complete")
    yield
    log(boot, "info", "shutdown")


app = FastAPI(
    title="E-commerce Mock API",
    version=__version__,
    description=(
        "Mock e-commerce platform for the hackathon sandbox. Every business route "
        "requires an `X-API-Key` header. Logs land as JSON lines in "
        "`/var/log/app/api.jsonl`; business events land in the `domain_events` table."
    ),
    lifespan=lifespan,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)

app.add_middleware(RequestLogMiddleware)

app.include_router(health.router)
app.include_router(products.router)
app.include_router(customers.router)
app.include_router(orders.router)


def _error_response(status_code: int, code: str, message: str, details: dict) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=get_context().request_id or None,
            details=details,
        )
    )
    # jsonable_encoder because `details` carries whatever the raising site put
    # there — datetimes, nested dicts, pydantic error objects.
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    # Surfacing the code here is what makes the access-log line self-describing.
    annotate(error_code=exc.code, **exc.details)
    response = _error_response(exc.status_code, exc.code, exc.message, exc.details)
    if isinstance(exc, RateLimited):
        response.headers["Retry-After"] = str(exc.retry_after)
    return response


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    annotate(error_code="validation_error", invalid_fields=len(exc.errors()))
    return _error_response(
        422,
        "validation_error",
        "request body or parameters failed validation",
        {"errors": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    annotate(error_code="internal_error", error_class=type(exc).__name__)
    logger.exception("unhandled_exception", extra={"event": "unhandled_exception"})
    return _error_response(500, "internal_error", "an unexpected error occurred", {})
