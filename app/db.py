"""SQLite storage for policies, catalog rows, orders, and webhook events."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


SHOPPER_ID = "shopper_demo"
SHOPPER_EMAIL = "demo@till.local"
SHOPPER_PASSWORD = "demo"

PAID_STATUSES = ("Approved", "OrderCreated", "PayerAction", "CaptureSubmitted", "Complete")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def start_of_utc_day() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return secrets.compare_digest(digest.hex(), digest_hex)


DEFAULT_POLICY = {
    "raw": "Running shoes, size 9, at most $80, no subscription.",
    "category": "running_shoes",
    "size": "9",
    "brand": None,
    "exclusions": ["subscription"],
    "max_purchase_minor": 8000,
    "daily_max_minor": 20000,
    "category_max_minor": 20000,
    "ask_above_minor": 8000,
    "currency": "USD",
    "no_subscription": True,
    "include_shipping": True,
    "shipping_counts_toward_cap": True,
    "allowed_merchants": [],
    "rule_citations": [],
    "intent_quantity": 1,
    "shipping_address": "",
}


def policy_with_expiry(ttl_seconds: int = 604800, base: dict | None = None) -> dict:
    rules = dict(base or DEFAULT_POLICY)
    rules["exclusions"] = list(rules.get("exclusions") or [])
    rules["allowed_merchants"] = list(rules.get("allowed_merchants") or [])
    rules["rule_citations"] = list(rules.get("rule_citations") or [])
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    rules["intent_expires_at"] = expires.replace(microsecond=0).isoformat()
    return rules


CATALOG = [
    {
        "sku": "SHOE-PEG-9",
        "merchant_id": "merchant_footlocker",
        "merchant_name": "Foot Locker",
        "title": "Nike Pegasus running shoes",
        "category": "running_shoes",
        "size": "9",
        "brand": "Nike",
        "unit_price_minor": 7400,
        "currency": "USD",
        "tax_minor": 0,
        "shipping_minor": 0,
        "addons_minor": 0,
        "subscription": 0,
    },
    {
        "sku": "SHOE-PEG-9-SHIP",
        "merchant_id": "merchant_footlocker",
        "merchant_name": "Foot Locker",
        "title": "Nike Pegasus running shoes",
        "category": "running_shoes",
        "size": "9",
        "brand": "Nike",
        "unit_price_minor": 7400,
        "currency": "USD",
        "tax_minor": 0,
        "shipping_minor": 900,
        "addons_minor": 0,
        "subscription": 0,
    },
    {
        "sku": "NECK-01",
        "merchant_id": "merchant_footlocker",
        "merchant_name": "Foot Locker",
        "title": "Gold necklace",
        "category": "jewelry",
        "size": None,
        "brand": None,
        "unit_price_minor": 7400,
        "currency": "USD",
        "tax_minor": 0,
        "shipping_minor": 0,
        "addons_minor": 0,
        "subscription": 0,
    },
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS shoppers (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  shopper_id TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
  version INTEGER PRIMARY KEY,
  raw_text TEXT NOT NULL,
  rules_json TEXT NOT NULL,
  active INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog (
  sku TEXT PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  merchant_name TEXT NOT NULL,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  size TEXT,
  brand TEXT,
  unit_price_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  tax_minor INTEGER NOT NULL,
  shipping_minor INTEGER NOT NULL,
  addons_minor INTEGER NOT NULL DEFAULT 0,
  subscription INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  shopper_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  policy_version INTEGER NOT NULL,
  policy_snapshot_json TEXT NOT NULL,
  merchant_id TEXT NOT NULL,
  sku TEXT NOT NULL,
  claimed_json TEXT NOT NULL,
  listed_json TEXT,
  canonical_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  approval_hash TEXT NOT NULL,
  approved_hash TEXT,
  approval_status TEXT NOT NULL,
  approval_source TEXT,
  approval_expires_at TEXT,
  status TEXT NOT NULL,
  decision TEXT NOT NULL,
  malicious INTEGER NOT NULL,
  problem TEXT,
  reasons_json TEXT NOT NULL,
  explanation TEXT NOT NULL,
  citation TEXT,
  payable_minor INTEGER NOT NULL,
  capped_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  address TEXT NOT NULL,
  paypal_order_id TEXT,
  paypal_capture_id TEXT,
  paypal_snapshot_json TEXT,
  approve_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  source_order_id TEXT NOT NULL,
  merchant_id TEXT,
  approval_hash TEXT,
  consumed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  order_id TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS captured_orders (
  paypal_order_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_window ON orders (shopper_id, merchant_id, sku, created_at);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM policies").fetchone()["n"]
    if count == 0:
        rules = policy_with_expiry()
        conn.execute(
            "INSERT INTO policies (version, raw_text, rules_json, active, created_at) VALUES (1, ?, ?, 1, ?)",
            (rules["raw"], json.dumps(rules), utcnow()),
        )
    catalog_count = conn.execute("SELECT COUNT(*) AS n FROM catalog").fetchone()["n"]
    if catalog_count == 0:
        conn.executemany(
            """
            INSERT INTO catalog (
              sku, merchant_id, merchant_name, title, category, size, brand,
              unit_price_minor, currency, tax_minor, shipping_minor, addons_minor, subscription
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["sku"],
                    item["merchant_id"],
                    item["merchant_name"],
                    item["title"],
                    item["category"],
                    item["size"],
                    item["brand"],
                    item["unit_price_minor"],
                    item["currency"],
                    item["tax_minor"],
                    item["shipping_minor"],
                    item["addons_minor"],
                    item["subscription"],
                )
                for item in CATALOG
            ],
        )
    shopper = conn.execute("SELECT id FROM shoppers WHERE id = ?", (SHOPPER_ID,)).fetchone()
    if shopper is None:
        conn.execute(
            "INSERT INTO shoppers (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (SHOPPER_ID, SHOPPER_EMAIL, hash_password(SHOPPER_PASSWORD), utcnow()),
        )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _reset_stale_schema(conn: sqlite3.Connection) -> None:
    orders = _columns(conn, "orders")
    catalog = _columns(conn, "catalog")
    stale = (orders and "shopper_id" not in orders) or (catalog and "addons_minor" not in catalog)
    if not stale:
        return
    conn.executescript(
        """
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS policies;
        DROP TABLE IF EXISTS catalog;
        DROP TABLE IF EXISTS learned_rules;
        DROP TABLE IF EXISTS webhook_events;
        DROP TABLE IF EXISTS audit;
        DROP TABLE IF EXISTS captured_orders;
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS shoppers;
        """
    )


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        _reset_stale_schema(conn)
        conn.executescript(SCHEMA)
        _seed(conn)
    finally:
        conn.close()


def reset_activity(path: str, ttl_seconds: int = 604800) -> None:
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM learned_rules")
        conn.execute("DELETE FROM webhook_events")
        conn.execute("DELETE FROM audit")
        conn.execute("DELETE FROM captured_orders")
        conn.execute("DELETE FROM policies")
        rules = policy_with_expiry(ttl_seconds)
        conn.execute(
            "INSERT INTO policies (version, raw_text, rules_json, active, created_at) VALUES (1, ?, ?, 1, ?)",
            (rules["raw"], json.dumps(rules), utcnow()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
