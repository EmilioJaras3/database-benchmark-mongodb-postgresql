#!/usr/bin/env python3
"""Stream deterministic synthetic orders as one JSON array."""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


DEFAULT_COUNT = 500_000
OUTPUT_PATH = Path("dataset/orders.json")
NAMESPACE = uuid.UUID("b8c0d90e-8041-4db7-9db6-e5f7c0725d0f")
BASE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

CATEGORIES = ("electronics", "home", "books", "clothing", "sports")
BRANDS = ("Northstar", "Cedarline", "Bluebird", "Mosaic", "Fieldworks")
TIERS = ("standard", "silver", "gold")
SEGMENTS = ("consumer", "small_business", "enterprise")
METHODS = ("card", "wallet", "bank_transfer")
CHANNELS = ("web", "mobile", "store")
TAX_PERCENTAGES = (5, 8, 10, 16)


def stable_id(kind, index):
    """Create repeatable UUIDs without storing or using real identities."""
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{index}"))


def money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_order(index, rng):
    # Seeded catalog values are synthetic and repeatable; each order has exactly three items.
    items = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    for _ in range(3):
        price = Decimal(rng.randint(500, 25_000)) / 100
        qty = rng.randint(1, 4)
        tax_pct = rng.choice(TAX_PERCENTAGES)
        subtotal += price * qty
        tax_total += money(price * qty * Decimal(tax_pct) / 100)
        items.append(
            {
                "sku": f"SKU-{rng.randint(10000, 99999)}",
                "category": rng.choice(CATEGORIES),
                "brand": rng.choice(BRANDS),
                "qty": qty,
                "price": float(price),
                "tax_pct": tax_pct,
                # The specification names discounts but does not define their element schema.
                "discounts": [],
            }
        )

    discount = Decimal("0.00")
    subtotal = money(subtotal)
    tax_total = money(tax_total)
    total = money(subtotal - discount + tax_total)
    location_id = index % 100
    timestamp = BASE_TIMESTAMP + timedelta(seconds=index * 17)
    completed = rng.random() < 0.8

    return {
        "order_id": stable_id("order", index),
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "customer": {
            "id": stable_id("customer", index % 100_000),
            "tier": rng.choice(TIERS),
            "segment": rng.choice(SEGMENTS),
            "geo": {
                "country": f"synthetic-country-{location_id:03d}",
                "state": f"synthetic-state-{location_id:03d}",
                "city": f"synthetic-city-{location_id:03d}",
                "zip": f"SYN-{location_id:05d}",
                # Numeric pairs are synthetic and do not identify real-world locations.
                "coordinates": [location_id - 50, (location_id * 37 % 361) - 180],
            },
        },
        "items": items,
        "payment": {
            "method": rng.choice(METHODS),
            "status": "completed" if completed else "pending",
            "transaction_id": stable_id("transaction", index),
            "gateway_latency_ms": rng.randint(20, 900),
            "totals": {
                "subtotal": float(subtotal),
                "discount": float(discount),
                "tax": float(tax_total),
                "total": float(total),
            },
        },
        "metadata": {
            "channel": rng.choice(CHANNELS),
            "user_agent": "SyntheticOrderGenerator/1.0",
            "session_id": stable_id("session", index),
            "flags": [],
        },
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-n", "--count", "--size", type=int, default=DEFAULT_COUNT,
        help=f"number of orders to generate (default: {DEFAULT_COUNT})",
    )
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("count must be a positive integer")
    return args


def main():
    args = parse_args()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects prior datasets from accidental replacement.
    with OUTPUT_PATH.open("x", encoding="utf-8", newline="\n") as stream:
        rng = random.Random(20261031)
        stream.write("[\n")
        for index in range(args.count):
            if index:
                stream.write(",\n")
            json.dump(generate_order(index, rng), stream, separators=(",", ":"), ensure_ascii=False)
        stream.write("\n]\n")
    print(f"Wrote {args.count} synthetic orders to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
