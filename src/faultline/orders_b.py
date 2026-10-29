"""Correct variant B: serializable transactions with bounded retries.

This is deliberately a different technique from correct-A, not a flag on it. A takes
row locks and relies on unique indexes. B creates no unique indexes and takes no row
locks; PostgreSQL's serializable snapshot isolation aborts any interleaving that could
not have happened serially, and B retries the whole transaction. A replay rebuilds the
original response from the stored, immutable order row.

Both variants must satisfy the same observable domain rules (specs/04-adapters.md); the
verifier never learns which variant ran.
"""

import random
import time
from uuid import uuid4

import psycopg
from psycopg import IsolationLevel
from psycopg.types.json import Jsonb

from faultline.models import digest
from faultline.orders import Rejection, connect

ATTEMPTS = 8
RESPONSE_FIELDS = (
    "order_id",
    "purchase_id",
    "account_id",
    "sku",
    "quantity",
    "amount_minor",
    "currency",
    "status",
)


class RetriesExhausted(psycopg.OperationalError):
    """Surfaces as a retryable 503; nothing was committed by the aborted attempts."""


def response(order: dict) -> dict:
    return {field: order[field] for field in RESPONSE_FIELDS}


def _once(payload: dict, key: str) -> dict:
    fingerprint = digest(payload)
    account_id = payload["account_id"]
    with connect() as conn:
        conn.isolation_level = IsolationLevel.SERIALIZABLE
        recorded = conn.execute(
            "SELECT fingerprint, order_id FROM idempotency WHERE account_id = %s AND key = %s",
            (account_id, key),
        ).fetchall()
        if recorded:
            if recorded[0]["fingerprint"] != fingerprint:
                raise Rejection("idempotency_conflict")
            stored = conn.execute(
                "SELECT * FROM orders WHERE order_id = %s", (recorded[0]["order_id"],)
            ).fetchone()
            return response(stored)
        same_purchase = conn.execute(
            "SELECT * FROM orders WHERE account_id = %s AND purchase_id = %s",
            (account_id, payload["purchase_id"]),
        ).fetchall()
        if same_purchase:
            order = same_purchase[0]
            if any(order[field] != value for field, value in payload.items()):
                raise Rejection("purchase_conflict")
            # Same purchase under a new key: remember the key, return the original result.
            conn.execute(
                "INSERT INTO idempotency VALUES (gen_random_uuid()::text, %s, %s, %s, %s)",
                (account_id, key, fingerprint, order["order_id"]),
            )
            return response(order)
        wallet = conn.execute(
            "SELECT balance_minor FROM accounts WHERE account_id = %s", (account_id,)
        ).fetchone()
        item = conn.execute(
            "SELECT price_minor, stock FROM products WHERE sku = %s", (payload["sku"],)
        ).fetchone()
        if wallet is None or item is None:
            raise Rejection("invalid_request", 422)
        amount = item["price_minor"] * payload["quantity"]
        if wallet["balance_minor"] < amount:
            raise Rejection("insufficient_funds")
        if item["stock"] < payload["quantity"]:
            raise Rejection("insufficient_stock")
        order = {
            **payload,
            "order_id": str(uuid4()),
            "amount_minor": amount,
            "currency": "INR",
            "status": "confirmed",
        }
        conn.execute(
            "INSERT INTO orders (order_id, purchase_id, account_id, sku, quantity, amount_minor, "
            "currency, status) VALUES (%(order_id)s, %(purchase_id)s, %(account_id)s, %(sku)s, "
            "%(quantity)s, %(amount_minor)s, %(currency)s, %(status)s)",
            order,
        )
        # Absolute writes are safe here only because serializable isolation rejects
        # any concurrent transaction that read the same row.
        conn.execute(
            "UPDATE accounts SET balance_minor = %s WHERE account_id = %s",
            (wallet["balance_minor"] - amount, account_id),
        )
        conn.execute(
            "UPDATE products SET stock = %s WHERE sku = %s",
            (item["stock"] - payload["quantity"], payload["sku"]),
        )
        conn.execute(
            "INSERT INTO ledger (entry_id, account_id, purchase_id, order_id, direction, "
            "amount_minor, currency) VALUES (gen_random_uuid()::text, %s, %s, %s, 'debit', %s, "
            "'INR')",
            (account_id, payload["purchase_id"], order["order_id"], amount),
        )
        conn.execute(
            "INSERT INTO movements (movement_id, sku, purchase_id, order_id, quantity_delta) "
            "VALUES (gen_random_uuid()::text, %s, %s, %s, %s)",
            (payload["sku"], payload["purchase_id"], order["order_id"], -payload["quantity"]),
        )
        conn.execute(
            "INSERT INTO outbox (event_id, purchase_id, order_id, event_type, payload) VALUES "
            "(gen_random_uuid()::text, %s, %s, 'order_confirmed', %s)",
            (payload["purchase_id"], order["order_id"], Jsonb(order)),
        )
        conn.execute(
            "INSERT INTO idempotency VALUES (gen_random_uuid()::text, %s, %s, %s, %s)",
            (account_id, key, fingerprint, order["order_id"]),
        )
    return order


def transact_serializable(payload: dict, key: str) -> dict:
    for attempt in range(ATTEMPTS):
        try:
            return _once(payload, key)
        except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
            # Jittered backoff keeps colliding retries from aborting each other again.
            time.sleep(random.uniform(0.005, 0.02) * (attempt + 1))
    raise RetriesExhausted("serialization retries exhausted")
