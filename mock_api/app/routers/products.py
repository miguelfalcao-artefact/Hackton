"""Catalog reads — the hot, cheap path that browsers hammer."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import ApiKey
from ..context import annotate
from ..db import get_db
from ..deps import require_api_key
from ..errors import NotFound
from ..models import Product
from ..schemas import Page, ProductOut
from ..util import require_uuid

router = APIRouter(prefix="/v1/products", tags=["products"])


@router.get("", response_model=Page[ProductOut])
def list_products(
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None, description="substring match on name or sku"),
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    in_stock: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page[ProductOut]:
    stmt = select(Product).where(Product.active.is_(True))

    if category:
        stmt = stmt.where(Product.category == category)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(Product.name.ilike(pattern) | Product.sku.ilike(pattern))
    if min_price is not None:
        stmt = stmt.where(Product.price_cents >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.price_cents <= max_price)
    if in_stock is not None:
        stmt = stmt.where(Product.stock > 0) if in_stock else stmt.where(Product.stock == 0)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Product.name).offset((page - 1) * page_size).limit(page_size)
    ).all()

    annotate(result_count=len(rows), total_matched=total)
    return Page[ProductOut](
        items=[ProductOut.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
        has_next=page * page_size < total,
    )


@router.get("/categories", response_model=list[str])
def list_categories(
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> list[str]:
    rows = db.scalars(
        select(Product.category).where(Product.active.is_(True)).distinct().order_by(Product.category)
    ).all()
    return list(rows)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: str,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(require_api_key),
) -> ProductOut:
    require_uuid(product_id, "product")
    product = db.get(Product, product_id)
    if product is None:
        raise NotFound("product not found", product_id=product_id)
    annotate(product_id=product.id, category=product.category)
    return ProductOut.model_validate(product)
