#!/usr/bin/env python3
"""
gen_api_logs.py — stream synthetic `ecommerce-api` logs into a local folder.

Collapses the `hackathon-two` sandbox (FastAPI + Postgres + Docker Compose, driven
over HTTP by a separate traffic generator) into a single dependency-free script.
The API is *simulated*, not served: no ports, no containers, no database. The two
log sinks are written directly.

    python gen_api_logs.py --out-dir logs

Schema is byte-compatible with the reference samples on the Desktop
(`sample-01-api.jsonl`, `sample-02-domain-events.jsonl`); the contract is
documented in `sample-05-LEIA-ME.md`. Anything built against those files reads
this output unchanged.

Two sinks, both rotating JSONL:

    logs/api.jsonl            `logger:"access"` request lines + `logger:"events"` mirror
    logs/domain-events.jsonl  the `domain_events` table rows

Ctrl+C stops the run and prints a stats summary to stderr.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import logging
import logging.handlers
import math
import os
import random
import signal
import sys
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Vocabulary — closed sets, taken verbatim from sample-05-LEIA-ME.md
# --------------------------------------------------------------------------- #

SERVICE = "ecommerce-api"
USER_AGENT = "traffic-gen/1.0.0"
CLIENT_IP = "172.18.0.1"  # docker bridge, constant in the reference samples

CATEGORIES = [
    "eletronicos",
    "informatica",
    "casa",
    "celulares",
    "moda",
    "cozinha",
    "pet",
]
CAT_PREFIX = {
    "eletronicos": "ELE",
    "informatica": "INF",
    "casa": "CAS",
    "celulares": "CEL",
    "moda": "MOD",
    "cozinha": "COZ",
    "pet": "PET",
}
SEGMENTS = ["new", "regular", "vip"]
CHANNELS = ["web", "mobile", "marketplace"]

# The nine documented event types and nothing else.
EVENT_TYPES = {
    "customer.registered": "customer",
    "order.created": "order",
    "order.paid": "order",
    "order.payment_declined": "order",
    "order.cancelled": "order",
    "order.returned": "order",
    "stock.depleted": "product",
    "stock.insufficient": "order",
    "ratelimit.exceeded": "api_key",
}

ERROR_CODES = {
    402: "payment_declined",
    404: "not_found",
    409: None,  # one of insufficient_stock / invalid_status_transition / duplicate_email
    422: "validation_error",
    429: "rate_limit_exceeded",
    500: "internal_error",
}

# Orders above this total are declined — mirrors `payment_decline_above_cents`
# in hackathon-two/app/config.py.
PAYMENT_DECLINE_ABOVE_CENTS = 500_000

TIER_LIMITS = {"enterprise": 6000, "pro": 600, "free": 60}

# id, tier, share of baseline traffic. quickcart/nightowl-bot are kept to a small
# share so the free-tier limit of 60/min is not tripped by baseline traffic —
# 429s must be a consequence of quota_hammer, not of normal operation.
API_KEY_SPECS = [
    ("acme-retail", "enterprise", 62),
    ("bolt-shop", "pro", 30),
    ("quickcart", "free", 5),
    ("nightowl-bot", "free", 3),
]

# Weights from sample-04-run-manifest.json.
PERSONA_WEIGHTS = {
    "browser": 55,
    "buyer": 25,
    "bulk_buyer": 5,
    "returner": 5,
    "abandoner": 7,
    "flaky_client": 3,
}

# Mean requests emitted per journey, used to convert --rps into a spawn rate.
AVG_STEPS = {
    "browser": 4.0,
    "buyer": 5.2,
    "bulk_buyer": 5.2,
    "returner": 6.2,
    "abandoner": 3.4,
    "flaky_client": 2.0,
}

# Median latency in ms per route, before jitter and anomaly multipliers.
ROUTE_LATENCY = {
    "/v1/products": 12.0,
    "/v1/products/{product_id}": 8.0,
    "/v1/orders": 30.0,
    "/v1/orders/{order_id}": 12.0,
    "/v1/orders/{order_id}/pay": 24.0,
    "/v1/orders/{order_id}/cancel": 20.0,
    "/v1/orders/{order_id}/return": 25.0,
    "/v1/customers": 20.0,
    "/healthz": 2.0,
}

TRACEBACKS = [
    'Traceback (most recent call last):\n  File "/srv/app/routers/orders.py", line 168, in list_orders\n'
    "    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0\n"
    "sqlalchemy.exc.OperationalError: (psycopg.OperationalError) connection failed: "
    "server closed the connection unexpectedly",
    'Traceback (most recent call last):\n  File "/srv/app/routers/orders.py", line 94, in create_order\n'
    "    db.commit()\n"
    "sqlalchemy.exc.IntegrityError: (psycopg.errors.DeadlockDetected) deadlock detected\n"
    "DETAIL:  Process 412 waits for ShareLock on transaction 88213",
    'Traceback (most recent call last):\n  File "/srv/app/routers/products.py", line 51, in get_product\n'
    "    row = db.execute(stmt).one_or_none()\n"
    "TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out",
]
EXC_CLASSES = ["OperationalError", "IntegrityError", "TimeoutError"]


def iso_ts(dt: datetime) -> str:
    """ISO 8601 UTC with milliseconds and a Z suffix, as the samples use."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #


class JsonlSink:
    """One rotating JSONL file.

    Wraps stdlib RotatingFileHandler with a bare "%(message)s" formatter and
    hands it pre-serialized JSON — the same trick hackathon-two/app/logging_setup.py
    uses, so rotation to .1/.2/... comes for free.
    """

    def __init__(self, path, max_bytes, backups, echo=False):
        self.path = path
        self.echo = echo
        self._handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        # A consumer holding the file open makes the rotation rename fail on
        # Windows. Degrade to "keep appending" instead of dying mid-stream.
        self._handler.handleError = self._on_handler_error
        self._rotation_warned = False

        self._logger = logging.getLogger("gen_api_logs.%s" % path.replace("\\", "/"))
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers = [self._handler]

    def _on_handler_error(self, record):  # noqa: ARG002 - stdlib signature
        if not self._rotation_warned:
            self._rotation_warned = True
            print(
                "warning: could not rotate %s (a reader may hold it open); "
                "continuing to append" % self.path,
                file=sys.stderr,
            )

    def write(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        self._logger.info(line)
        if self.echo:
            print(line, flush=True)

    def close(self):
        self._handler.flush()
        self._handler.close()


class Sinks:
    """The two sinks, written together so ordering across them is guaranteed."""

    def __init__(self, out_dir, max_bytes, backups, echo=False):
        os.makedirs(out_dir, exist_ok=True)
        self.api = JsonlSink(
            os.path.join(out_dir, "api.jsonl"), max_bytes, backups, echo
        )
        self.events = JsonlSink(
            os.path.join(out_dir, "domain-events.jsonl"), max_bytes, backups, echo
        )
        self._next_event_id = itertools.count(4101)  # continues the sample's numbering

    def access(self, line):
        self.api.write(line)

    def domain_event(self, ts, event_type, aggregate_id, request_id, api_key_id, payload):
        """Write one business event to both sinks.

        `api.jsonl` gets the `logger:"events"` mirror with the payload flattened;
        `domain-events.jsonl` gets the table row with the payload nested. Called
        after the access line for the same request_id, so a consumer never sees
        an event before the request that caused it.
        """
        aggregate_type = EVENT_TYPES[event_type]
        mirror = {
            "ts": ts,
            "level": "INFO",
            "service": SERVICE,
            "logger": "events",
            "event": event_type,
            "msg": event_type,
            "request_id": request_id,
            "api_key_id": api_key_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        }
        mirror.update(payload)
        self.api.write(mirror)
        self.events.write(
            {
                "id": next(self._next_event_id),
                "ts": ts,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "request_id": request_id,
                "api_key_id": api_key_id,
                "payload": payload,
            }
        )

    def close(self):
        self.api.close()
        self.events.close()


# --------------------------------------------------------------------------- #
# World
# --------------------------------------------------------------------------- #


class Product:
    __slots__ = ("id", "sku", "category", "price_cents", "stock")

    def __init__(self, pid, sku, category, price_cents, stock):
        self.id = pid
        self.sku = sku
        self.category = category
        self.price_cents = price_cents
        self.stock = stock


class Order:
    __slots__ = ("id", "customer_id", "total_cents", "status", "item_count", "unit_count")

    def __init__(self, oid, customer_id, total_cents, item_count, unit_count):
        self.id = oid
        self.customer_id = customer_id
        self.total_cents = total_cents
        self.status = "pending"
        self.item_count = item_count
        self.unit_count = unit_count


class ApiKey:
    __slots__ = ("id", "tier", "weight", "limit_per_min")

    def __init__(self, kid, tier, weight, limit_per_min):
        self.id = kid
        self.tier = tier
        self.weight = weight
        self.limit_per_min = limit_per_min


class RateLimiter:
    """Per-key sliding window over the last 60 seconds.

    Real, not scripted: baseline traffic stays under the limit and quota_hammer
    blows through it, so every 429 in the log is a consequence of actual request
    density on that key.
    """

    def __init__(self, keys):
        self.limits = {k.id: k.limit_per_min for k in keys}
        self.windows = {k.id: deque() for k in keys}

    def check(self, key_id, now):
        """Return None if allowed, else `retry_after` seconds."""
        if key_id not in self.windows:
            return None
        dq = self.windows[key_id]
        while dq and now - dq[0] >= 60.0:
            dq.popleft()
        if len(dq) >= self.limits[key_id]:
            return max(1, int(math.ceil(60.0 - (now - dq[0]))))
        dq.append(now)
        return None


class World:
    def __init__(self, rng, rps):
        self.rng = rng
        self.products = []
        self.by_category = {}
        for cat in CATEGORIES:
            items = []
            for i in range(rng.randint(30, 60)):
                items.append(
                    Product(
                        str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                        "SKU-%s-%04d" % (CAT_PREFIX[cat], i + 1),
                        cat,
                        rng.randrange(2_000, 40_001, 100),
                        rng.randint(40, 400),
                    )
                )
            self.by_category[cat] = items
            self.products.extend(items)

        self.customers = [
            str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(40)
        ]
        self.segment_of = {c: rng.choice(SEGMENTS) for c in self.customers}
        self.orders = {}

        # Free-tier limit is 60/min at the default --rps 10, exactly as the
        # samples show. At higher rates it scales with the key's actual share so
        # the baseline stays clean; the emitted `limit_per_min` is always the
        # real limit in force.
        total_weight = sum(w for _, _, w in API_KEY_SPECS)
        self.api_keys = []
        for kid, tier, weight in API_KEY_SPECS:
            share_rpm = rps * (weight / total_weight) * 60.0
            limit = max(TIER_LIMITS[tier], int(math.ceil(share_rpm * 2.0)))
            self.api_keys.append(ApiKey(kid, tier, weight, limit))
        self.key_weights = [k.weight for k in self.api_keys]
        self.limiter = RateLimiter(self.api_keys)

        # Products that stock_run is currently draining.
        self.stock_run_targets = []

    def pick_key(self):
        return self.rng.choices(self.api_keys, weights=self.key_weights, k=1)[0]

    def key_by_id(self, kid):
        for k in self.api_keys:
            if k.id == kid:
                return k
        raise KeyError(kid)

    def pick_customer(self):
        return self.rng.choice(self.customers)

    def new_customer(self):
        cid = str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        self.customers.append(cid)
        self.segment_of[cid] = self.rng.choice(SEGMENTS)
        return cid


# --------------------------------------------------------------------------- #
# Journeys — each is a generator of (kind, params, think_seconds).
# The engine executes the step against the world and sends the result back in,
# so a journey can branch on what actually happened (a 402 ends a purchase).
# --------------------------------------------------------------------------- #


def _cart(rng, products, n_items, max_units):
    items = []
    for p in rng.sample(products, min(n_items, len(products))):
        items.append((p, rng.randint(1, max_units)))
    return items


def journey_browser(w, ctx):
    cat = w.rng.choice(CATEGORIES)
    yield ("list_products", {"category": cat, "page": 1, "page_size": 20}, w.rng.uniform(0.4, 1.4))
    for _ in range(w.rng.randint(2, 4)):
        p = w.rng.choice(w.by_category[cat])
        yield ("get_product", {"product": p}, w.rng.uniform(0.4, 1.6))


def _purchase_prelude(w, ctx):
    """Shared opening for the buying personas: optional signup, browse, detail."""
    if w.rng.random() < 0.15:
        res = yield ("register_customer", {}, w.rng.uniform(0.4, 1.2))
        if res.get("status") == 201:
            ctx["customer_id"] = res["customer_id"]
    ctx.setdefault("customer_id", w.pick_customer())
    cat = w.rng.choice(CATEGORIES)
    ctx["category"] = cat
    yield ("list_products", {"category": cat, "page": 1, "page_size": 20}, w.rng.uniform(0.4, 1.3))
    yield ("get_product", {"product": w.rng.choice(w.by_category[cat])}, w.rng.uniform(0.4, 1.5))


def journey_buyer(w, ctx, bulk=False, returner=False):
    yield from _purchase_prelude(w, ctx)
    if ctx.get("high_value"):
        pool = sorted(w.products, key=lambda p: -p.price_cents)[:40]
        items = _cart(w.rng, pool, w.rng.randint(1, 3), 3)
    elif bulk:
        pool = w.stock_run_targets or w.by_category[ctx["category"]]
        items = _cart(w.rng, pool, w.rng.randint(1, 3), 12)
    else:
        items = _cart(w.rng, w.by_category[ctx["category"]], w.rng.randint(1, 3), 4)

    res = yield ("create_order", {"items": items, "customer_id": ctx["customer_id"]}, w.rng.uniform(0.5, 1.6))
    if res.get("status") != 201:
        return
    oid = res["order_id"]
    res = yield ("pay_order", {"order_id": oid}, w.rng.uniform(0.3, 1.2))
    if res.get("status") != 200:
        return
    yield ("get_order", {"order_id": oid}, w.rng.uniform(0.3, 1.0))
    if returner:
        yield ("return_order", {"order_id": oid}, w.rng.uniform(0.5, 1.5))


def journey_bulk_buyer(w, ctx):
    yield from journey_buyer(w, ctx, bulk=True)


def journey_returner(w, ctx):
    yield from journey_buyer(w, ctx, returner=True)


def journey_abandoner(w, ctx):
    """Creates an order and never pays.

    Present at baseline on purpose — it is what makes mass_abandon detectable as
    a shift in the created/paid ratio rather than as a brand-new event.
    """
    yield from _purchase_prelude(w, ctx)
    items = _cart(w.rng, w.by_category[ctx["category"]], w.rng.randint(1, 3), 4)
    res = yield ("create_order", {"items": items, "customer_id": ctx["customer_id"]}, w.rng.uniform(0.6, 2.0))
    if res.get("status") == 201 and w.rng.random() < 0.3:
        yield ("cancel_order", {"order_id": res["order_id"]}, w.rng.uniform(0.5, 1.5))


def journey_flaky_client(w, ctx):
    """Produces the ordinary client-error background: 422, 404, invalid transitions."""
    roll = w.rng.random()
    if roll < 0.35:
        yield ("create_order", {"items": [], "malformed": True}, w.rng.uniform(0.3, 1.0))
    elif roll < 0.70:
        yield ("get_product", {"product": None, "bad_id": True}, w.rng.uniform(0.3, 1.0))
    elif roll < 0.85:
        yield ("list_orders", {}, w.rng.uniform(0.3, 1.0))
    else:
        paid = [o for o in w.orders.values() if o.status == "paid"]
        if paid:
            yield ("pay_order", {"order_id": w.rng.choice(paid).id}, w.rng.uniform(0.3, 1.0))
        else:
            yield ("register_customer", {"duplicate": True}, w.rng.uniform(0.3, 1.0))


def journey_health(w, ctx):
    yield ("health", {}, 0.0)


def journey_scraper(w, ctx):
    """scraper_burst: walks the catalogue, page 1..8 at page_size 100, buys nothing."""
    cat = w.rng.choice(CATEGORIES)
    for page in range(1, 9):
        yield ("list_products", {"category": cat, "page": page, "page_size": 100}, 0.05)


def journey_quota_hammer(w, ctx):
    """quota_hammer: one key hitting product detail flat out until the limiter trips."""
    for _ in range(25):
        yield ("get_product", {"product": w.rng.choice(w.products)}, 0.02)


def journey_retry_storm(w, ctx):
    """retry_storm: the same cart POSTed five times in about half a second.

    All five share one base request_id with suffixes -01..-05, and each one
    succeeds — so the signature is five duplicate orders, not five errors.
    """
    ctx["customer_id"] = w.pick_customer()
    cat = w.rng.choice(CATEGORIES)
    items = _cart(w.rng, w.by_category[cat], 2, 3)
    for _ in range(5):
        yield ("create_order", {"items": items, "customer_id": ctx["customer_id"]}, 0.13)


JOURNEYS = {
    "browser": journey_browser,
    "buyer": journey_buyer,
    "bulk_buyer": journey_bulk_buyer,
    "returner": journey_returner,
    "abandoner": journey_abandoner,
    "flaky_client": journey_flaky_client,
    "health": journey_health,
    "scraper": journey_scraper,
    "quota_hammer": journey_quota_hammer,
    "retry_storm": journey_retry_storm,
}


class Journey:
    __slots__ = ("gen", "base_id", "step", "persona", "key", "ctx", "pending")

    def __init__(self, persona, gen, base_id, key, ctx):
        self.persona = persona
        self.gen = gen
        self.base_id = base_id
        self.key = key
        self.ctx = ctx
        self.step = 0
        self.pending = None

    def request_id(self):
        return "%s-%02d" % (self.base_id, self.step)


# --------------------------------------------------------------------------- #
# Anomalies — the documented seven, with the durations from
# sample-04-run-manifest.json.
# --------------------------------------------------------------------------- #


class Anomaly:
    name = "none"
    duration = 0.0

    def spawn_multiplier(self):
        return 1.0

    def latency_multiplier(self, rng):
        return 1.0

    def persona_weights(self, base):
        return base

    def high_value_orders(self):
        return False

    def inject(self, engine, now):
        """Spawn dedicated journeys. Called on every engine tick."""

    def start(self, engine):
        pass

    def stop(self, engine):
        pass


class TrafficSpike(Anomaly):
    name, duration = "traffic_spike", 45.0

    def spawn_multiplier(self):
        return 8.0

    def latency_multiplier(self, rng):
        # Takes the ~10ms baseline into the documented 45-210ms band.
        return rng.uniform(4.5, 21.0)


class ScraperBurst(Anomaly):
    name, duration = "scraper_burst", 60.0

    def __init__(self):
        self._next = 0.0

    def inject(self, engine, now):
        if now >= self._next:
            engine.spawn("scraper", key_id="nightowl-bot")
            self._next = now + 0.6


class QuotaHammer(Anomaly):
    name, duration = "quota_hammer", 40.0

    def __init__(self):
        self._next = 0.0

    def inject(self, engine, now):
        if now >= self._next:
            engine.spawn("quota_hammer", key_id="quickcart")
            self._next = now + 0.55


class FraudRing(Anomaly):
    name, duration = "fraud_ring", 45.0

    def high_value_orders(self):
        return True

    def persona_weights(self, base):
        w = dict(base)
        w["buyer"] = 70
        w["browser"] = 20
        return w


class StockRun(Anomaly):
    name, duration = "stock_run", 50.0
    n_skus = 5

    def __init__(self):
        self._next = 0.0

    def start(self, engine):
        w = engine.world
        engine.world.stock_run_targets = w.rng.sample(w.products, self.n_skus)
        for p in engine.world.stock_run_targets:
            p.stock = min(p.stock, w.rng.randint(6, 25))

    def stop(self, engine):
        engine.world.stock_run_targets = []

    def inject(self, engine, now):
        if now >= self._next:
            engine.spawn("bulk_buyer")
            self._next = now + 0.7


class RetryStorm(Anomaly):
    name, duration = "retry_storm", 40.0

    def __init__(self):
        self._next = 0.0

    def inject(self, engine, now):
        if now >= self._next:
            engine.spawn("retry_storm")
            self._next = now + 1.6


class MassAbandon(Anomaly):
    name, duration = "mass_abandon", 45.0

    def persona_weights(self, base):
        # order.created volume holds; order.paid disappears. Every request still
        # returns 200/201 — only the missing payments give it away.
        return {"abandoner": 80, "browser": 20}


ANOMALY_CLASSES = [
    TrafficSpike,
    ScraperBurst,
    QuotaHammer,
    FraudRing,
    StockRun,
    RetryStorm,
    MassAbandon,
]
ANOMALY_NAMES = [c.name for c in ANOMALY_CLASSES]
NO_ANOMALY = Anomaly()


class AnomalyScheduler:
    """Cycles the selected anomalies forever, separated by `calm` seconds."""

    def __init__(self, names, calm, rng):
        self.classes = [c for c in ANOMALY_CLASSES if c.name in names]
        self.calm = calm
        self.rng = rng
        self.current = NO_ANOMALY
        self.fired = Counter()
        self._idx = 0
        self._next_start = calm if self.classes else float("inf")
        self._end = 0.0

    def tick(self, engine, elapsed):
        if self.current is not NO_ANOMALY:
            if elapsed >= self._end:
                self.current.stop(engine)
                engine.note("anomaly ended:   %s" % self.current.name)
                self.current = NO_ANOMALY
                self._next_start = elapsed + self.calm
            else:
                self.current.inject(engine, elapsed)
            return
        if elapsed >= self._next_start:
            cls = self.classes[self._idx % len(self.classes)]
            self._idx += 1
            self.current = cls()
            self._end = elapsed + self.current.duration
            self.fired[self.current.name] += 1
            self.current.start(engine)
            engine.note(
                "anomaly started: %s (%.0fs)" % (self.current.name, self.current.duration)
            )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class Stats:
    MAX_LAT = 400_000

    def __init__(self):
        self.requests = 0
        self.by_status = Counter()
        self.by_error = Counter()
        self.by_event = Counter()
        self.journeys = Counter()
        self.latencies = []

    def record(self, status, latency_ms, error_code):
        self.requests += 1
        self.by_status[status] += 1
        if error_code:
            self.by_error[error_code] += 1
        if len(self.latencies) < self.MAX_LAT:
            self.latencies.append(latency_ms)

    def percentile(self, q):
        if not self.latencies:
            return 0.0
        xs = sorted(self.latencies)
        k = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * q))))
        return round(xs[k], 2)


class Engine:
    def __init__(self, world, sinks, sched, args, rng):
        self.world = world
        self.sinks = sinks
        self.sched = sched
        self.args = args
        self.rng = rng
        self.stats = Stats()
        self.heap = []
        self.seq = itertools.count()
        self.start_mono = time.monotonic()
        self.quiet = args.quiet
        base_avg = sum(
            PERSONA_WEIGHTS[p] * AVG_STEPS[p] for p in PERSONA_WEIGHTS
        ) / sum(PERSONA_WEIGHTS.values())
        self.base_spawn_rate = args.rps / base_avg
        self._next_spawn = 0.0
        self._next_health = 5.0

    # -- helpers ---------------------------------------------------------- #

    def note(self, msg):
        if not self.quiet:
            print(
                "[%6.1fs] %s" % (time.monotonic() - self.start_mono, msg),
                file=sys.stderr,
            )

    def elapsed(self):
        return time.monotonic() - self.start_mono

    def spawn(self, persona, key_id=None):
        ctx = {}
        if self.sched.current.high_value_orders() and persona in (
            "buyer",
            "bulk_buyer",
            "returner",
        ):
            ctx["high_value"] = True
        key = self.world.key_by_id(key_id) if key_id else self.world.pick_key()
        if persona == "health":
            key = None
        base_id = "%024x" % self.rng.getrandbits(96)
        gen = JOURNEYS[persona](self.world, ctx)
        j = Journey(persona, gen, base_id, key, ctx)
        self.stats.journeys[persona] += 1
        self._advance(j, None, self.elapsed())

    def _advance(self, journey, result, now):
        """Pull the next step out of the journey and schedule it."""
        try:
            step = journey.gen.send(result) if journey.step else next(journey.gen)
        except StopIteration:
            return
        kind, params, think = step
        journey.pending = (kind, params)
        heapq.heappush(self.heap, (now + think, next(self.seq), journey))

    # -- request execution ------------------------------------------------ #

    def _latency(self, route, status):
        if status == 429:
            return round(self.rng.uniform(2.8, 6.2), 2)
        if status == 500:
            return round(self.rng.uniform(3800, 5600), 2)
        base = ROUTE_LATENCY[route]
        lat = base * math.exp(self.rng.gauss(0.0, 0.35))
        lat *= self.sched.current.latency_multiplier(self.rng)
        return round(max(0.5, lat), 2)

    def execute(self, journey, now):
        kind, params = journey.pending
        journey.step += 1
        rid = journey.request_id()
        key = journey.key

        handler = getattr(self, "_h_" + kind)
        method, route, path, extras, events, status = handler(journey, params)

        # Rate limit check happens before the handler's effects would be visible
        # to a consumer, so a 429 replaces the outcome entirely.
        retry_after = None
        if key is not None and status < 400:
            retry_after = self.world.limiter.check(key.id, now)
            if retry_after is not None:
                status = 429
                extras = {}
                events = [
                    (
                        "ratelimit.exceeded",
                        key.id,
                        {
                            "tier": key.tier,
                            "limit_per_min": key.limit_per_min,
                            "path": path,
                        },
                    )
                ]

        # Rare unhandled exception, independent of any injected anomaly.
        if status < 400 and self.rng.random() < self.args.baseline_5xx_rate:
            status, extras, events = 500, extras, []

        error_code = None
        if status >= 400:
            error_code = extras.pop("_error_code", None) or ERROR_CODES.get(status)

        latency_ms = self._latency(route, status)
        level = "INFO" if status < 400 else ("WARNING" if status < 500 else "ERROR")
        ts = iso_ts(datetime.now(timezone.utc))

        line = {
            "ts": ts,
            "level": level,
            "service": SERVICE,
            "logger": "access",
            "event": "http_request",
            "msg": "http_request",
            "request_id": rid,
            "method": method,
            "path": path,
            "route": route,
            "status": status,
            "latency_ms": latency_ms,
            "api_key_id": key.id if key else None,
            "tier": key.tier if key else None,
            "user_agent": USER_AGENT,
            "client_ip": CLIENT_IP,
        }
        line.update(extras)
        if error_code:
            line["error_code"] = error_code
        if status == 429:
            line["limit_per_min"] = key.limit_per_min
            line["retry_after"] = retry_after or 60
        if status == 500:
            i = self.rng.randrange(len(TRACEBACKS))
            line["error_class"] = EXC_CLASSES[i]
            line["exc"] = TRACEBACKS[i]

        self.sinks.access(line)
        self.stats.record(status, latency_ms, error_code)

        for event_type, aggregate_id, payload in events:
            self.sinks.domain_event(
                ts, event_type, aggregate_id, rid, key.id if key else None, payload
            )
            self.stats.by_event[event_type] += 1

        result = {"status": status}
        result.update({k: v for k, v in extras.items() if not k.startswith("_")})
        self._advance(journey, result, now)

    # -- handlers: each returns (method, route, path, extras, events, status) -- #

    def _h_list_products(self, journey, p):
        cat = p["category"]
        page, size = p["page"], p["page_size"]
        matched = len(self.world.by_category[cat])
        count = max(0, min(size, matched - (page - 1) * size))
        return (
            "GET",
            "/v1/products",
            "/v1/products",
            {
                "query": "category=%s&page=%d&page_size=%d" % (cat, page, size),
                "result_count": count,
                "total_matched": matched,
            },
            [],
            200,
        )

    def _h_get_product(self, journey, p):
        if p.get("bad_id"):
            pid = str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
            return (
                "GET",
                "/v1/products/{product_id}",
                "/v1/products/%s" % pid,
                {},
                [],
                404,
            )
        prod = p["product"]
        return (
            "GET",
            "/v1/products/{product_id}",
            "/v1/products/%s" % prod.id,
            {"product_id": prod.id, "category": prod.category},
            [],
            200,
        )

    def _h_create_order(self, journey, p):
        route = path = "/v1/orders"
        if p.get("malformed"):
            return (
                "POST",
                route,
                path,
                {"invalid_fields": self.rng.randint(1, 3)},
                [],
                422,
            )

        items = p["items"]
        cid = p["customer_id"]
        shortages = [
            {"product_id": prod.id, "requested": qty, "available": prod.stock}
            for prod, qty in items
            if prod.stock < qty
        ]
        if shortages:
            return (
                "POST",
                route,
                path,
                {"shortages": shortages, "_error_code": "insufficient_stock"},
                [
                    (
                        "stock.insufficient",
                        None,
                        {"customer_id": cid, "shortages": shortages},
                    )
                ],
                409,
            )

        total = sum(prod.price_cents * qty for prod, qty in items)
        if journey.ctx.get("high_value") and total <= PAYMENT_DECLINE_ABOVE_CENTS:
            # fraud_ring: push the cart over the decline threshold so payment
            # fails for a reason a consumer can actually see in the payload.
            total = self.rng.randrange(510_000, 800_001, 100)

        oid = str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        order = Order(oid, cid, total, len(items), sum(q for _, q in items))
        self.world.orders[oid] = order

        events = [
            (
                "order.created",
                oid,
                {
                    "customer_id": cid,
                    "segment": self.world.segment_of.get(cid, "regular"),
                    "channel": self.rng.choice(CHANNELS),
                    "total_cents": total,
                    "item_count": order.item_count,
                    "unit_count": order.unit_count,
                },
            )
        ]
        for prod, qty in items:
            prod.stock -= qty
            if prod.stock == 0:
                events.append(
                    (
                        "stock.depleted",
                        prod.id,
                        {"sku": prod.sku, "category": prod.category, "order_id": oid},
                    )
                )
        return (
            "POST",
            route,
            path,
            {"order_id": oid, "customer_id": cid, "amount_cents": total},
            events,
            201,
        )

    def _h_pay_order(self, journey, p):
        oid = p["order_id"]
        route = "/v1/orders/{order_id}/pay"
        path = "/v1/orders/%s/pay" % oid
        order = self.world.orders.get(oid)
        if order is None:
            return "POST", route, path, {}, [], 404
        if order.status != "pending":
            return (
                "POST",
                route,
                path,
                {
                    "order_id": oid,
                    "order_status": order.status,
                    "_error_code": "invalid_status_transition",
                },
                [],
                409,
            )

        base = {
            "order_id": oid,
            "customer_id": order.customer_id,
            "amount_cents": order.total_cents,
        }
        if order.total_cents > PAYMENT_DECLINE_ABOVE_CENTS:
            extras = dict(base)
            extras.update(
                {
                    "decline_reason": "declined_token",
                    "_error_code": "payment_declined",
                    "reason": "declined_token",
                    "total_cents": order.total_cents,
                }
            )
            return (
                "POST",
                route,
                path,
                extras,
                [
                    (
                        "order.payment_declined",
                        oid,
                        {
                            "customer_id": order.customer_id,
                            "total_cents": order.total_cents,
                            "method": "card",
                            "reason": "declined_token",
                        },
                    )
                ],
                402,
            )

        order.status = "paid"
        return (
            "POST",
            route,
            path,
            base,
            [
                (
                    "order.paid",
                    oid,
                    {
                        "customer_id": order.customer_id,
                        "total_cents": order.total_cents,
                        "method": "card",
                    },
                )
            ],
            200,
        )

    def _h_get_order(self, journey, p):
        oid = p["order_id"]
        route = "/v1/orders/{order_id}"
        path = "/v1/orders/%s" % oid
        order = self.world.orders.get(oid)
        if order is None:
            return "GET", route, path, {}, [], 404
        return (
            "GET",
            route,
            path,
            {
                "order_id": oid,
                "customer_id": order.customer_id,
                "order_status": order.status,
            },
            [],
            200,
        )

    def _h_cancel_order(self, journey, p):
        oid = p["order_id"]
        route = "/v1/orders/{order_id}/cancel"
        path = "/v1/orders/%s/cancel" % oid
        order = self.world.orders.get(oid)
        if order is None:
            return "POST", route, path, {}, [], 404
        if order.status != "pending":
            return (
                "POST",
                route,
                path,
                {
                    "order_id": oid,
                    "order_status": order.status,
                    "_error_code": "invalid_status_transition",
                },
                [],
                409,
            )
        previous, order.status = order.status, "cancelled"
        return (
            "POST",
            route,
            path,
            {"order_id": oid, "order_status": "cancelled"},
            [
                (
                    "order.cancelled",
                    oid,
                    {
                        "previous_status": previous,
                        "total_cents": order.total_cents,
                        "reason": "customer_request",
                    },
                )
            ],
            200,
        )

    def _h_return_order(self, journey, p):
        oid = p["order_id"]
        route = "/v1/orders/{order_id}/return"
        path = "/v1/orders/%s/return" % oid
        order = self.world.orders.get(oid)
        if order is None:
            return "POST", route, path, {}, [], 404
        if order.status != "paid":
            return (
                "POST",
                route,
                path,
                {
                    "order_id": oid,
                    "order_status": order.status,
                    "_error_code": "invalid_status_transition",
                },
                [],
                409,
            )
        order.status = "returned"
        return (
            "POST",
            route,
            path,
            {"order_id": oid, "order_status": "returned"},
            [
                (
                    "order.returned",
                    oid,
                    {
                        "customer_id": order.customer_id,
                        "total_cents": order.total_cents,
                        "reason": self.rng.choice(
                            ["damaged", "wrong_item", "no_longer_needed"]
                        ),
                    },
                )
            ],
            200,
        )

    def _h_list_orders(self, journey, p):
        return (
            "GET",
            "/v1/orders",
            "/v1/orders",
            {"query": "page=1&page_size=20", "result_count": min(20, len(self.world.orders))},
            [],
            200,
        )

    def _h_register_customer(self, journey, p):
        route = path = "/v1/customers"
        if p.get("duplicate"):
            return (
                "POST",
                route,
                path,
                {"_error_code": "duplicate_email"},
                [],
                409,
            )
        cid = self.world.new_customer()
        email = "user%s@example.com" % cid[:8]
        return (
            "POST",
            route,
            path,
            {"customer_id": cid},
            [
                (
                    "customer.registered",
                    cid,
                    {
                        "email": email,
                        "country": self.rng.choice(["BR", "BR", "BR", "PT", "AR"]),
                        "segment": self.world.segment_of[cid],
                    },
                )
            ],
            201,
        )

    def _h_health(self, journey, p):
        return "GET", "/healthz", "/healthz", {}, [], 200

    # -- main loop -------------------------------------------------------- #

    def run(self, stop_flag):
        while not stop_flag():
            now = self.elapsed()
            if self.args.duration and now >= self.args.duration:
                break

            self.sched.tick(self, now)

            rate = self.base_spawn_rate * self.sched.current.spawn_multiplier()
            weights = self.sched.current.persona_weights(PERSONA_WEIGHTS)
            names = list(weights)
            wts = [weights[n] for n in names]

            while self._next_spawn <= now:
                self.spawn(self.rng.choices(names, weights=wts, k=1)[0])
                self._next_spawn += self.rng.expovariate(rate)

            if now >= self._next_health:
                self.spawn("health")
                self._next_health = now + 10.0

            while self.heap and self.heap[0][0] <= now:
                _, _, journey = heapq.heappop(self.heap)
                self.execute(journey, now)

            nxt = min(
                self._next_spawn,
                self._next_health,
                self.heap[0][0] if self.heap else float("inf"),
            )
            time.sleep(max(0.002, min(0.05, nxt - self.elapsed())))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_anomalies(spec):
    spec = spec.strip().lower()
    if spec == "all":
        return list(ANOMALY_NAMES)
    if spec == "none":
        return []
    names = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [n for n in names if n not in ANOMALY_NAMES]
    if unknown:
        raise argparse.ArgumentTypeError(
            "unknown anomaly %s; choose from: %s"
            % (", ".join(unknown), ", ".join(ANOMALY_NAMES))
        )
    return names


def build_parser():
    p = argparse.ArgumentParser(
        prog="gen_api_logs.py",
        description="Stream synthetic ecommerce-api logs into a local folder.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--out-dir", default="logs", help="folder for the log files")
    p.add_argument("--rps", type=float, default=10.0, help="baseline requests per second")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    p.add_argument("--no-seed", action="store_true", help="use a random seed")
    p.add_argument(
        "--anomalies",
        type=parse_anomalies,
        default="all",
        help="all | none | comma-separated: " + ",".join(ANOMALY_NAMES),
    )
    p.add_argument(
        "--calm", type=float, default=60.0, help="clean seconds between anomalies"
    )
    p.add_argument(
        "--duration", type=float, default=0.0, help="seconds to run; 0 = until Ctrl+C"
    )
    p.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024, help="rotation size")
    p.add_argument("--backups", type=int, default=5, help="rotated files to keep")
    p.add_argument("--echo", action="store_true", help="mirror every line to stdout")
    p.add_argument(
        "--baseline-5xx-rate",
        type=float,
        default=0.003,
        help="background unhandled-exception rate; 0 disables",
    )
    p.add_argument("--quiet", action="store_true", help="no progress notes on stderr")
    p.add_argument(
        "--manifest",
        action="store_true",
        help="also write run-manifest.json ground truth (off by default)",
    )
    return p


def print_summary(engine, out):
    s = engine.stats
    classes = Counter()
    for status, n in s.by_status.items():
        classes["%dxx" % (status // 100)] += n
    print("", file=out)
    print("-- run summary -----------------------------------------", file=out)
    print("requests        %d in %.1fs (%.2f rps)"
          % (s.requests, engine.elapsed(), s.requests / max(engine.elapsed(), 1e-9)), file=out)
    print("status_classes  %s" % dict(sorted(classes.items())), file=out)
    print("by_status       %s" % dict(sorted(s.by_status.items())), file=out)
    print("by_error        %s" % dict(s.by_error.most_common()), file=out)
    print("latency_ms      p50=%s p95=%s p99=%s"
          % (s.percentile(0.50), s.percentile(0.95), s.percentile(0.99)), file=out)
    print("journeys        %s" % dict(s.journeys.most_common()), file=out)
    print("domain_events   %s" % dict(s.by_event.most_common()), file=out)
    created = s.by_event.get("order.created", 0)
    paid = s.by_event.get("order.paid", 0)
    print("paid/created    %.3f" % (paid / created) if created else "paid/created    n/a",
          file=out)
    print("anomalies       %s" % dict(engine.sched.fired.most_common()), file=out)
    print("--------------------------------------------------------", file=out)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if isinstance(args.anomalies, str):
        args.anomalies = parse_anomalies(args.anomalies)

    seed = None if args.no_seed else args.seed
    rng = random.Random(seed)

    world = World(rng, args.rps)
    sinks = Sinks(args.out_dir, args.max_bytes, args.backups, args.echo)
    sched = AnomalyScheduler(args.anomalies, args.calm, rng)
    engine = Engine(world, sinks, sched, args, rng)

    stopping = {"flag": False}

    def on_sigint(signum, frame):  # noqa: ARG001
        if stopping["flag"]:
            raise KeyboardInterrupt
        stopping["flag"] = True
        print("\nstopping — draining…", file=sys.stderr)

    signal.signal(signal.SIGINT, on_sigint)

    if not args.quiet:
        print("writing to %s/{api.jsonl,domain-events.jsonl}" % args.out_dir, file=sys.stderr)
        print(
            "rps=%s seed=%s anomalies=%s calm=%.0fs"
            % (args.rps, seed, ",".join(args.anomalies) or "none", args.calm),
            file=sys.stderr,
        )
        print(
            "rate limits  %s"
            % {k.id: k.limit_per_min for k in world.api_keys},
            file=sys.stderr,
        )
        print("Ctrl+C to stop.", file=sys.stderr)

    started_at = iso_ts(datetime.now(timezone.utc))
    try:
        engine.run(lambda: stopping["flag"])
    except KeyboardInterrupt:
        pass
    finally:
        sinks.close()

    if args.manifest:
        import os

        manifest = {
            "run_id": "stream-%s" % started_at.replace(":", "").replace("-", "")[:15],
            "scenario": "stream",
            "seed": seed,
            "started_at": started_at,
            "ended_at": iso_ts(datetime.now(timezone.utc)),
            "duration_s": round(engine.elapsed(), 1),
            "base_rps": args.rps,
            "persona_weights": PERSONA_WEIGHTS,
            "anomalies_fired": dict(sched.fired),
            "stats": {
                "requests": engine.stats.requests,
                "by_status": dict(sorted(engine.stats.by_status.items())),
                "by_error": dict(engine.stats.by_error),
                "latency_ms": {
                    "p50": engine.stats.percentile(0.50),
                    "p95": engine.stats.percentile(0.95),
                    "p99": engine.stats.percentile(0.99),
                },
            },
        }
        path = os.path.join(args.out_dir, "run-manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        print("wrote %s" % path, file=sys.stderr)

    print_summary(engine, sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
