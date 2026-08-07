"""Deterministic dataset.

Seeded with a fixed value so every teammate's database is byte-identical.
That matters: a run of the traffic generator is only reproducible if the
catalog it shops from is the same everywhere.
"""
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Customer, Order, OrderItem, OrderStatus, Product

logger = logging.getLogger("seed")

CATEGORIES = [
    "eletronicos",
    "informatica",
    "celulares",
    "casa",
    "cozinha",
    "moda",
    "esportes",
    "beleza",
    "livros",
    "brinquedos",
    "pet",
    "automotivo",
]

# (noun pool, price floor in cents, price ceiling in cents) per category.
_CATALOG: dict[str, tuple[list[str], int, int]] = {
    "eletronicos": (["Smart TV", "Soundbar", "Fone Bluetooth", "Caixa de Som", "Projetor"], 19900, 899900),
    "informatica": (["Notebook", "Monitor", "Teclado Mecanico", "Mouse Gamer", "SSD"], 9900, 1299900),
    "celulares": (["Smartphone", "Capa", "Carregador Turbo", "Power Bank", "Pelicula"], 2900, 999900),
    "casa": (["Aspirador", "Ventilador", "Luminaria", "Organizador", "Cortina"], 3900, 249900),
    "cozinha": (["Air Fryer", "Liquidificador", "Cafeteira", "Panela", "Jogo de Facas"], 4900, 189900),
    "moda": (["Camiseta", "Tenis", "Jaqueta", "Calca Jeans", "Bone"], 3900, 79900),
    "esportes": (["Bicicleta", "Halter", "Tapete Yoga", "Bola", "Corda"], 2900, 549900),
    "beleza": (["Perfume", "Secador", "Kit Skincare", "Batom", "Shampoo"], 1900, 89900),
    "livros": (["Romance", "Biografia", "Tecnico", "Infantil", "HQ"], 1900, 19900),
    "brinquedos": (["Quebra-Cabeca", "Carrinho", "Boneca", "Lego", "Jogo de Tabuleiro"], 2900, 99900),
    "pet": (["Racao", "Coleira", "Brinquedo Pet", "Cama Pet", "Comedouro"], 1900, 39900),
    "automotivo": (["Pneu", "Oleo", "Som Automotivo", "Capa Banco", "Kit Ferramentas"], 4900, 299900),
}

_BRANDS = ["Nova", "Vertex", "Lumen", "Orbita", "Kaizen", "Atlas", "Zenit", "Prisma", "Ceres", "Nimbus"]
_FIRST = ["Ana", "Bruno", "Carla", "Diego", "Elisa", "Felipe", "Gabriela", "Henrique", "Isabela", "Joao",
          "Karina", "Lucas", "Marina", "Nelson", "Olivia", "Paulo", "Rafaela", "Sergio", "Tatiana", "Vitor"]
_LAST = ["Silva", "Souza", "Costa", "Pereira", "Almeida", "Rocha", "Lima", "Barros", "Cardoso", "Teixeira"]
_COUNTRIES = ["BR"] * 16 + ["PT", "AR", "CL", "MX"]

N_PRODUCTS = 400
N_CUSTOMERS = 200
N_ORDERS = 300

# Historical status mix. Enough `delivered` rows that the returner persona has
# something to act on from the very first request.
_STATUS_MIX = (
    [OrderStatus.DELIVERED] * 40
    + [OrderStatus.SHIPPED] * 15
    + [OrderStatus.PAID] * 20
    + [OrderStatus.PENDING] * 12
    + [OrderStatus.CANCELLED] * 8
    + [OrderStatus.RETURNED] * 5
)


def is_empty(db: Session) -> bool:
    return (db.scalar(select(func.count()).select_from(Product)) or 0) == 0


def seed(db: Session) -> dict[str, int]:
    rng = random.Random(settings.seed_value)
    now = datetime.now(timezone.utc)

    products = _seed_products(db, rng, now)
    customers = _seed_customers(db, rng, now)
    db.flush()
    orders = _seed_orders(db, rng, now, products, customers)
    db.commit()

    counts = {"products": len(products), "customers": len(customers), "orders": len(orders)}
    logger.info("seeded %s", counts)
    return counts


def _seed_products(db: Session, rng: random.Random, now: datetime) -> list[Product]:
    products: list[Product] = []
    for i in range(N_PRODUCTS):
        category = CATEGORIES[i % len(CATEGORIES)]
        nouns, low, high = _CATALOG[category]
        noun = rng.choice(nouns)
        brand = rng.choice(_BRANDS)
        price = rng.randrange(low, high + 1, 100)

        # One product in eight ships nearly sold out, so `insufficient_stock`
        # and `stock.depleted` show up without anyone forcing them.
        stock = rng.randint(0, 3) if i % 8 == 0 else rng.randint(20, 500)

        product = Product(
            sku=f"SKU-{category[:3].upper()}-{i:04d}",
            name=f"{noun} {brand} {rng.choice(['Pro', 'Max', 'Lite', 'Plus', 'S', 'X'])}",
            category=category,
            price_cents=price,
            currency="BRL",
            stock=stock,
            active=(i % 40 != 0),  # a few discontinued items
            created_at=now - timedelta(days=rng.randint(30, 400)),
        )
        db.add(product)
        products.append(product)
    return products


def _seed_customers(db: Session, rng: random.Random, now: datetime) -> list[Customer]:
    customers: list[Customer] = []
    for i in range(N_CUSTOMERS):
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        customer = Customer(
            email=f"{first.lower()}.{last.lower()}{i:03d}@example.com",
            name=f"{first} {last}",
            country=rng.choice(_COUNTRIES),
            segment=rng.choices(["new", "regular", "vip"], weights=[30, 55, 15])[0],
            created_at=now - timedelta(days=rng.randint(1, 500)),
        )
        db.add(customer)
        customers.append(customer)
    return customers


def _seed_orders(
    db: Session,
    rng: random.Random,
    now: datetime,
    products: list[Product],
    customers: list[Customer],
) -> list[Order]:
    sellable = [p for p in products if p.active and p.stock > 0]
    orders: list[Order] = []

    for _ in range(N_ORDERS):
        customer = rng.choice(customers)
        status = rng.choice(_STATUS_MIX)
        created = now - timedelta(
            days=rng.randint(0, 30), hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
        )

        order = Order(
            customer_id=customer.id,
            status=status.value,
            channel=rng.choices(["web", "mobile", "marketplace"], weights=[55, 35, 10])[0],
            currency="BRL",
            total_cents=0,
            created_at=created,
            updated_at=created + timedelta(hours=rng.randint(0, 48)),
        )
        db.add(order)
        db.flush()

        total = 0
        for product in rng.sample(sellable, rng.randint(1, 4)):
            qty = rng.randint(1, 3)
            line_total = product.price_cents * qty
            total += line_total
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=qty,
                    unit_price_cents=product.price_cents,
                    line_total_cents=line_total,
                )
            )
            # Historical orders that were not cancelled or returned consumed stock.
            if status not in (OrderStatus.CANCELLED, OrderStatus.RETURNED):
                product.stock = max(0, product.stock - qty)

        order.total_cents = total
        orders.append(order)

    return orders
