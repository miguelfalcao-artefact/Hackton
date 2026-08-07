"""Engine, session factory, and startup wait-for-database."""
import logging
import time
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=5,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db() -> None:
    """Block until Postgres answers, or give up after the configured retries.

    compose's `service_healthy` already gates startup, but a restarted API
    container can still race the database; this keeps the boot deterministic.
    """
    last: Exception | None = None
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            last = exc
            logger.warning("database not ready (attempt %s), retrying", attempt)
            time.sleep(settings.db_connect_backoff_s)
    raise RuntimeError(f"database never became reachable: {last}")


def create_all(drop_first: bool = False) -> None:
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
