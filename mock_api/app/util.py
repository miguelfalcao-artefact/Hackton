"""Small helpers shared by the routers."""
import uuid

from .errors import NotFound


def valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def require_uuid(value: str, what: str) -> str:
    """Reject a malformed id before Postgres does.

    Handing a non-UUID string to a UUID column raises a DataError, which would
    surface as a 500. A client sending garbage deserves a 404, not an incident.
    """
    if not valid_uuid(value):
        raise NotFound(f"{what} not found", **{f"{what}_id": value, "reason": "malformed_id"})
    return value
