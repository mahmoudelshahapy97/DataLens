"""The built-in demo database.

The zero-configuration path: with no ``VANNA_DATABASE_URL`` the stack seeds a small
SQLite file and answers questions against that, so ``docker compose up`` works with
an empty ``.env``.

Deliberately shaped to exercise the features. ``region``, ``tier``, ``status`` and
``category`` are all low-cardinality, so the scanner captures their real values and
the agent never has to guess a filter literal. ``amount_cents`` is in cents to give
the instruction store something real to correct.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("vanna.demo")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    region       TEXT NOT NULL,
    tier         TEXT NOT NULL,
    signed_up_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    price_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    product_id   INTEGER NOT NULL REFERENCES products(id),
    status       TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    ordered_on   TEXT NOT NULL
);
"""


def seed_demo_database(path: str) -> None:
    """Create the demo database if none exists. Idempotent."""
    file = Path(path)
    if file.exists() and file.stat().st_size > 0:
        return

    logger.info("Seeding demo database at %s", path)
    file.parent.mkdir(parents=True, exist_ok=True)

    import random

    random.seed(42)  # reproducible demo data

    regions = ["EMEA", "AMER", "APAC"]
    tiers = ["free", "pro", "enterprise"]
    statuses = ["PENDING", "SHIPPED", "DELIVERED", "CANCELLED"]
    categories = ["Hardware", "Software", "Services"]

    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO customers VALUES (?,?,?,?,?)",
            [
                (
                    i,
                    f"Customer {i:03d}",
                    regions[i % 3],
                    tiers[i % 3],
                    f"202{4 + i % 2}-{(i % 12) + 1:02d}-15",
                )
                for i in range(1, 121)
            ],
        )
        connection.executemany(
            "INSERT INTO products VALUES (?,?,?,?)",
            [
                (i, f"Product {i:02d}", categories[i % 3], (i * 1999) % 250_000 + 999)
                for i in range(1, 25)
            ],
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
            [
                (
                    i,
                    (i % 120) + 1,
                    (i % 24) + 1,
                    statuses[i % 4],
                    (i % 5) + 1,
                    ((i % 5) + 1) * (((i * 1999) % 250_000) + 999),
                    f"2026-{(i % 8) + 1:02d}-{(i % 28) + 1:02d}",
                )
                for i in range(1, 2001)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    logger.info("Demo database seeded: 120 customers, 24 products, 2000 orders")
