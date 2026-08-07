"""Structured JSON logging to stdout and to a rotating file inside the container.

The file sink is the point of the whole exercise: whatever consumes these logs
later reads `/var/log/app/api.jsonl`, one JSON object per line.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .config import settings

# Attribute names already present on every LogRecord. Anything a caller passes
# through `extra=` that collides with these would raise, so `log()` filters them.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", record.name),
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update({k: v for k, v in fields.items() if v is not None})
        if record.exc_info:
            payload["error_class"] = record.exc_info[0].__name__
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(service: str | None = None) -> logging.Logger:
    service = service or settings.service_name
    formatter = JsonFormatter(service)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)

    if settings.log_to_file:
        try:
            os.makedirs(settings.log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                os.path.join(settings.log_dir, "api.jsonl"),
                maxBytes=settings.log_file_max_bytes,
                backupCount=settings.log_file_backups,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            # A read-only or missing log volume must never take the API down.
            root.warning("could not open log file, continuing with stdout only")

    # uvicorn's own access log would duplicate our structured one.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

    return logging.getLogger(service)


def log(logger: logging.Logger, level: str, event: str, **fields) -> None:
    """Emit a structured record: log(logger, "info", "order_created", order_id=...)."""
    safe = {k: v for k, v in fields.items() if k not in _RESERVED}
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        event,
        extra={"event": event, "fields": safe},
    )
