"""Orders: creation, the status machine, and the money path.

Every rejection here is deterministic. Nothing is randomised, so a run of the
traffic generator with a fixed seed produces the same errors every time.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import events
from ..auth import ApiKey
from ..config import settings
from ..context import annotate
from ..db import get_db
from ..deps import require_api_key
from ..errors import InsufficientStock, InvalidTransition, NotFound, PaymentDeclined
from ..models import ORDER_TRANSITIONS, Customer, Order, OrderItem, OrderStatus, Product
from ..schemas import OrderCreate, OrderOut, Page, PaymentRequest, ReasonRequest
from ..util import require_uuid

router = APIRouter(prefix="/v1/orders", tags=["orders"])

DECLINE_TOKEN_PREFIX = "tok_decline"


def _load_order(db: Session, order_id: str) -> Order:
    require_uuid(order_id, "order")
    order = db.get(Order, order_id)
    if order is None:
        raise NotFound("order not found", order_id=order_id)
    return order


def _check_transition(order: Order, target: OrderStatus) -> None:
    current = OrderStatus(order.status)
    if target not in ORDER_TRANSITIONS[current]:
        raise InvalidTransition(
            f"cannot move order from {current.value} to {target.value}",
            order_id=order.id,
            current_status=current.value,
            requested_status=target.value,
        )


def _transition(order: Order, target: OrderStatus) -> None:
    _check_transition(order, target)
    order.status = target.value


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    body: OrderCreate,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    require_uuid(body.customer_id, "customer")
    customer = db.get(Customer, body.customer_id)
    if customer is None:
        raise NotFound("customer not found", customer_id=body.customer_id)

    for item in body.items:
        require_uuid(item.product_id, "product")

    # Collapse duplicate lines so "2 × A" and "1 × A, 1 × A" hit stock the same way.
    wanted: dict[str, int] = {}
    for item in body.items:
        wanted[item.product_id] = wanted.get(item.product_id, 0) + item.quantity

    # Lock the rows we are about to decrement, in a stable order so concurrent
    # orders touching the same SKUs queue up instead of deadlocking.
    product_ids = sorted(wanted)
    products = db.scalars(
        select(Product).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
    ).all()
    by_id = {p.id: p for p in products}

    missing = [pid for pid in product_ids if pid not in by_id]
    if missing:
        db.rollback()
        raise NotFound("product not found", product_id=missing[0], missing_count=len(missing))

    short = [
        {"product_id": pid, "requested": qty, "available": by_id[pid].stock}
        for pid, qty in wanted.items()
        if by_id[pid].stock < qty or not by_id[pid].active
    ]
    if short:
        events.emit(
            db,
            events.STOCK_INSUFFICIENT,
            aggregate_type="order",
            aggregate_id=None,
            customer_id=customer.id,
            shortages=short,
        )
        db.commit()
        raise InsufficientStock(
            "one or more items are unavailable in the requested quantity",
            shortages=short,
        )

    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PENDING.value,
        channel=body.channel,
        currency=next(iter(by_id.values())).currency,
        total_cents=0,
    )
    db.add(order)
    db.flush()

    total = 0
    depleted: list[Product] = []
    for product_id, qty in wanted.items():
        product = by_id[product_id]
        line_total = product.price_cents * qty
        total += line_total
        product.stock -= qty
        if product.stock == 0:
            depleted.append(product)
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=qty,
                unit_price_cents=product.price_cents,
                line_total_cents=line_total,
            )
        )

    order.total_cents = total

    events.emit(
        db,
        events.ORDER_CREATED,
        aggregate_type="order",
        aggregate_id=order.id,
        customer_id=customer.id,
        segment=customer.segment,
        channel=order.channel,
        total_cents=total,
        item_count=len(wanted),
        unit_count=sum(wanted.values()),
    )
    for product in depleted:
        events.emit(
            db,
            events.STOCK_DEPLETED,
            aggregate_type="product",
            aggregate_id=product.id,
            sku=product.sku,
            category=product.category,
            order_id=order.id,
        )

    db.commit()
    db.refresh(order)

    annotate(order_id=order.id, customer_id=customer.id, amount_cents=total)
    return OrderOut.model_validate(order)


@router.get("", response_model=Page[OrderOut])
def list_orders(
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page[OrderOut]:
    stmt = select(Order)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)
    if customer_id:
        require_uuid(customer_id, "customer")
        stmt = stmt.where(Order.customer_id == customer_id)
    if since:
        stmt = stmt.where(Order.created_at >= since)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Order.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    annotate(result_count=len(rows), total_matched=total)
    return Page[OrderOut](
        items=[OrderOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
        has_next=page * page_size < total,
    )


@router.get("/{order_id}", response_model=OrderOut)
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    order = _load_order(db, order_id)
    annotate(order_id=order.id, customer_id=order.customer_id, order_status=order.status)
    return OrderOut.model_validate(order)


@router.post("/{order_id}/pay", response_model=OrderOut)
def pay_order(
    order_id: str,
    body: PaymentRequest,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    order = _load_order(db, order_id)
    # Reachability first: paying an already-paid order is a 409, not a 402.
    _check_transition(order, OrderStatus.PAID)

    declined_reason = None
    if body.payment_token.startswith(DECLINE_TOKEN_PREFIX):
        declined_reason = "declined_token"
    elif order.total_cents > settings.payment_decline_above_cents:
        declined_reason = "amount_above_ceiling"

    if declined_reason:
        # The order stays pending — a declined payment is not a state change.
        events.emit(
            db,
            events.ORDER_PAYMENT_DECLINED,
            aggregate_type="order",
            aggregate_id=order.id,
            customer_id=order.customer_id,
            total_cents=order.total_cents,
            method=body.method,
            reason=declined_reason,
        )
        db.commit()
        annotate(
            order_id=order.id,
            customer_id=order.customer_id,
            amount_cents=order.total_cents,
            decline_reason=declined_reason,
        )
        raise PaymentDeclined(
            "payment was declined",
            order_id=order.id,
            reason=declined_reason,
            total_cents=order.total_cents,
        )

    order.status = OrderStatus.PAID.value
    events.emit(
        db,
        events.ORDER_PAID,
        aggregate_type="order",
        aggregate_id=order.id,
        customer_id=order.customer_id,
        total_cents=order.total_cents,
        method=body.method,
    )
    db.commit()
    db.refresh(order)

    annotate(order_id=order.id, customer_id=order.customer_id, amount_cents=order.total_cents)
    return OrderOut.model_validate(order)


@router.post("/{order_id}/cancel", response_model=OrderOut)
def cancel_order(
    order_id: str,
    body: ReasonRequest | None = None,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    order = _load_order(db, order_id)
    previous = order.status
    _transition(order, OrderStatus.CANCELLED)

    # Cancelling releases the reservation.
    _restock(db, order)

    events.emit(
        db,
        events.ORDER_CANCELLED,
        aggregate_type="order",
        aggregate_id=order.id,
        customer_id=order.customer_id,
        previous_status=previous,
        total_cents=order.total_cents,
        reason=(body.reason if body else "unspecified"),
    )
    db.commit()
    db.refresh(order)

    annotate(order_id=order.id, customer_id=order.customer_id, order_status=order.status)
    return OrderOut.model_validate(order)


@router.post("/{order_id}/return", response_model=OrderOut)
def return_order(
    order_id: str,
    body: ReasonRequest | None = None,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    order = _load_order(db, order_id)
    _transition(order, OrderStatus.RETURNED)

    _restock(db, order)

    events.emit(
        db,
        events.ORDER_RETURNED,
        aggregate_type="order",
        aggregate_id=order.id,
        customer_id=order.customer_id,
        total_cents=order.total_cents,
        reason=(body.reason if body else "unspecified"),
    )
    db.commit()
    db.refresh(order)

    annotate(order_id=order.id, customer_id=order.customer_id, order_status=order.status)
    return OrderOut.model_validate(order)


# ------------------------------------------------------------------ fulfilment
# Ship and deliver exist so a live order can reach `delivered`, which is the
# only state a return can start from. Without them the returner persona could
# only ever act on seeded history.


@router.post("/{order_id}/ship", response_model=OrderOut)
def ship_order(
    order_id: str,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    order = _load_order(db, order_id)
    _transition(order, OrderStatus.SHIPPED)
    db.commit()
    db.refresh(order)
    annotate(order_id=order.id, order_status=order.status)
    return OrderOut.model_validate(order)


@router.post("/{order_id}/deliver", response_model=OrderOut)
def deliver_order(
    order_id: str,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> OrderOut:
    order = _load_order(db, order_id)
    _transition(order, OrderStatus.DELIVERED)
    db.commit()
    db.refresh(order)
    annotate(order_id=order.id, order_status=order.status)
    return OrderOut.model_validate(order)


def _restock(db: Session, order: Order) -> None:
    """Put the reserved units back on the shelf."""
    for item in order.items:
        product = db.get(Product, item.product_id, with_for_update=True)
        if product is not None:
            product.stock += item.quantity
