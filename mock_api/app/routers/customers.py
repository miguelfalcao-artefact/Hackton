"""Customer registration and lookup."""
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import events
from ..auth import ApiKey
from ..context import annotate
from ..db import get_db
from ..deps import require_api_key
from ..errors import DuplicateEmail, NotFound
from ..models import Customer, Order
from ..schemas import CustomerCreate, CustomerOut, OrderOut, Page
from ..util import require_uuid

router = APIRouter(prefix="/v1/customers", tags=["customers"])


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    body: CustomerCreate,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> CustomerOut:
    customer = Customer(
        email=str(body.email).lower(),
        name=body.name,
        country=body.country.upper(),
        segment=body.segment,
    )
    db.add(customer)
    try:
        # The INSERT goes out here, so this is where the unique violation
        # surfaces — not at commit.
        db.flush()
    except IntegrityError:
        db.rollback()
        raise DuplicateEmail("a customer with this email already exists", email=str(body.email))

    events.emit(
        db,
        events.CUSTOMER_REGISTERED,
        aggregate_type="customer",
        aggregate_id=customer.id,
        email=customer.email,
        country=customer.country,
        segment=customer.segment,
    )
    db.commit()

    annotate(customer_id=customer.id)
    return CustomerOut.model_validate(customer)


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(
    customer_id: str,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> CustomerOut:
    require_uuid(customer_id, "customer")
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFound("customer not found", customer_id=customer_id)
    annotate(customer_id=customer.id)
    return CustomerOut.model_validate(customer)


@router.get("/{customer_id}/orders", response_model=Page[OrderOut])
def list_customer_orders(
    customer_id: str,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page[OrderOut]:
    require_uuid(customer_id, "customer")
    if db.get(Customer, customer_id) is None:
        raise NotFound("customer not found", customer_id=customer_id)

    stmt = select(Order).where(Order.customer_id == customer_id)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Order.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    annotate(customer_id=customer_id, result_count=len(rows))
    return Page[OrderOut](
        items=[OrderOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
        has_next=page * page_size < total,
    )
