"""Business-event sink: writes to `domain_events` and mirrors to the JSON log.

`emit` does NOT commit — it adds to the caller's session so the event lands in
the same transaction as the state change it describes. If the mutation rolls
back, the event goes with it.
"""
import logging

from sqlalchemy.orm import Session

from .context import get_context
from .logging_setup import log
from .models import DomainEvent

logger = logging.getLogger("events")

# Every business event this API can emit. Kept in one place so whoever consumes
# the logs later has a closed vocabulary to work against.
CUSTOMER_REGISTERED = "customer.registered"
ORDER_CREATED = "order.created"
ORDER_PAID = "order.paid"
ORDER_PAYMENT_DECLINED = "order.payment_declined"
ORDER_CANCELLED = "order.cancelled"
ORDER_RETURNED = "order.returned"
STOCK_DEPLETED = "stock.depleted"
STOCK_INSUFFICIENT = "stock.insufficient"
RATELIMIT_EXCEEDED = "ratelimit.exceeded"

EVENT_TYPES = [
    CUSTOMER_REGISTERED,
    ORDER_CREATED,
    ORDER_PAID,
    ORDER_PAYMENT_DECLINED,
    ORDER_CANCELLED,
    ORDER_RETURNED,
    STOCK_DEPLETED,
    STOCK_INSUFFICIENT,
    RATELIMIT_EXCEEDED,
]


def emit(
    db: Session,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | None = None,
    **payload,
) -> DomainEvent:
    ctx = get_context()
    event = DomainEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        request_id=ctx.request_id or None,
        api_key_id=ctx.api_key_id,
        payload=payload,
    )
    db.add(event)

    # The same fact in both sinks: the file for stream consumers, the table for
    # SQL consumers. Merged as a dict so a payload key can never collide with
    # one of the envelope fields and raise.
    log(
        logger,
        "info",
        event_type,
        **{
            "request_id": ctx.request_id or None,
            "api_key_id": ctx.api_key_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            **payload,
        },
    )
    return event
