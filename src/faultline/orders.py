"""The actual PostgreSQL-backed reference service and its independent fixture reader."""

import os
import time
from typing import Annotated
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from faultline.models import digest

TABLES = ("accounts", "products", "orders", "ledger", "movements", "outbox", "idempotency")
SCHEMA = """
CREATE TABLE accounts (account_id text PRIMARY KEY, balance_minor bigint NOT NULL);
CREATE TABLE products (sku text PRIMARY KEY, price_minor bigint NOT NULL, stock bigint NOT NULL);
CREATE TABLE orders (
    order_id text PRIMARY KEY, purchase_id text NOT NULL, account_id text NOT NULL,
    sku text NOT NULL, quantity bigint NOT NULL, amount_minor bigint NOT NULL,
    currency text NOT NULL, status text NOT NULL
);
CREATE TABLE ledger (
    entry_id text PRIMARY KEY, account_id text NOT NULL, purchase_id text NOT NULL,
    order_id text NOT NULL, direction text NOT NULL, amount_minor bigint NOT NULL,
    currency text NOT NULL
);
CREATE TABLE movements (
    movement_id text PRIMARY KEY, sku text NOT NULL, purchase_id text NOT NULL,
    order_id text NOT NULL, quantity_delta bigint NOT NULL
);
CREATE TABLE outbox (
    event_id text PRIMARY KEY, purchase_id text NOT NULL, order_id text NOT NULL,
    event_type text NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE idempotency (
    record_id text PRIMARY KEY, account_id text NOT NULL, key text NOT NULL,
    fingerprint text NOT NULL, order_id text NOT NULL
);
CREATE TABLE inflight (
    registration_id text PRIMARY KEY, process_epoch text NOT NULL,
    backend_pid integer NOT NULL, backend_start timestamptz NOT NULL
);
CREATE TABLE fixture_identity (name text PRIMARY KEY);
"""
# Application-side seed data. The verifier keeps its own independent expectation.
SEEDS = {
    "standard_wallet": {
        "accounts": [("buyer-a", 1000000)],
        "products": [("sku-a", 200000, 10)],
    },
    "reduction_market": {
        "accounts": [("buyer-a", 1000000)]
        + [(f"buyer-{index:02d}", 1000000) for index in range(1, 20)],
        "products": [("sku-a", 200000, 10), ("sku-noise", 10000, 100)],
    },
}


def connect(role: str = "app", *, autocommit: bool = False):
    return psycopg.connect(
        host=os.environ.get("FL_DB_HOST", "database") if role == "app" else "database",
        port=int(os.environ.get("FL_DB_PORT", "5432")) if role == "app" else 5432,
        dbname="faultline",
        user=f"fl_{role}",
        password=os.environ[f"FL_{role.upper()}_PASSWORD"],
        connect_timeout=3,
        application_name=f"faultline_{role}",
        autocommit=autocommit,
        row_factory=dict_row,
        options="-c statement_timeout=5000 -c lock_timeout=5000",
    )


def prepare(variant: str, fixture: str = "standard_wallet") -> dict:
    if variant not in {"unsafe", "correct"} or fixture not in SEEDS:
        raise ValueError("unsupported variant or fixture")
    with connect("setup") as conn:
        conn.execute(SCHEMA)
        conn.execute("INSERT INTO fixture_identity VALUES (%s)", (fixture,))
        for account in SEEDS[fixture]["accounts"]:
            conn.execute("INSERT INTO accounts VALUES (%s, %s)", account)
        for product in SEEDS[fixture]["products"]:
            conn.execute("INSERT INTO products VALUES (%s, %s, %s)", product)
        if variant == "correct":
            conn.execute("CREATE UNIQUE INDEX purchase_identity ON orders(account_id, purchase_id)")
            conn.execute("CREATE UNIQUE INDEX retry_identity ON idempotency(account_id, key)")
        for role in ("app", "observer"):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(f"fl_{role}"),
                    sql.Literal(os.environ[f"FL_{role.upper()}_PASSWORD"]),
                )
            )
        conn.execute("GRANT USAGE ON SCHEMA public TO fl_app, fl_observer")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO fl_app")
        conn.execute("GRANT DELETE ON inflight TO fl_app")
        # The application has no reason to read or change which fixture was seeded.
        conn.execute("REVOKE ALL ON fixture_identity FROM fl_app")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO fl_observer")
        conn.execute("GRANT pg_read_all_stats TO fl_observer")
        conn.execute("ALTER ROLE fl_observer SET default_transaction_read_only = on")
    return {"prepared": True}


def snapshot(role: str) -> dict:
    started = time.time_ns()
    with connect("observer") as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        records = {
            table: conn.execute(
                sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
            ).fetchall()
            for table in TABLES
        }
        transaction = conn.execute("SELECT pg_current_snapshot()::text AS id").fetchone()["id"]
        fixture = conn.execute("SELECT name FROM fixture_identity").fetchall()
        sessions = conn.execute(
            "SELECT count(*) AS count FROM pg_stat_activity WHERE usename = 'fl_app'"
        ).fetchone()["count"]
    return {
        "snapshot_schema_version": 1,
        "snapshot_id": role,
        "role": role,
        "adapter_profile": "orders_v1",
        # A missing or ambiguous identity is reported, never assumed.
        "fixture": fixture[0]["name"] if len(fixture) == 1 else "unidentified",
        "authority": "postgresql",
        "isolation": "repeatable_read_read_only",
        "started_ns": started,
        "completed_ns": time.time_ns(),
        "transaction": transaction,
        "complete": True,
        "quiescent": sessions == 0,
        "application_sessions": sessions,
        "records": records,
        "row_counts": {key: len(value) for key, value in records.items()},
        "content_hash": digest(records),
    }


def registry_evidence():
    with connect("observer") as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = conn.execute(
            "SELECT registration_id, process_epoch, backend_pid, backend_start::text, "
            "EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.pid = i.backend_pid "
            "AND a.backend_start = i.backend_start) AS session_alive FROM inflight i "
            "ORDER BY registration_id"
        ).fetchall()
        sessions = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename='fl_app'"
        ).fetchone()["n"]
    return {"registrations": rows, "application_sessions": sessions}


def clear_stale_registry():
    # Called only after owned-container exit is confirmed. Recheck database sessions
    # inside this helper before deleting diagnostic rows; business tables are untouched.
    with connect("setup") as conn:
        if conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename='fl_app'"
        ).fetchone()["n"]:
            raise ValueError("application sessions still active")
        cleared = conn.execute("DELETE FROM inflight RETURNING registration_id").fetchall()
    return {"cleared": sorted(row["registration_id"] for row in cleared)}


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    purchase_id: Annotated[str, Field(min_length=1, max_length=64)]
    account_id: Annotated[str, Field(min_length=1, max_length=64)]
    sku: Annotated[str, Field(min_length=1, max_length=64)]
    quantity: Annotated[int, Field(ge=1, le=2147483647)]


class Rejection(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status


def transact_order(payload: dict, key: str, variant: str) -> dict:
    """Return only after the database transaction has committed."""
    fingerprint = digest(payload)
    with connect() as conn:
        account = conn.execute(
            "SELECT * FROM accounts WHERE account_id = %s FOR UPDATE", (payload["account_id"],)
        ).fetchone()
        if account is None:
            raise Rejection("invalid_request", 422)
        existing = None
        if variant == "correct":
            token = conn.execute(
                "SELECT * FROM idempotency WHERE account_id = %s AND key = %s",
                (payload["account_id"], key),
            ).fetchone()
            if token and token["fingerprint"] != fingerprint:
                raise Rejection("idempotency_conflict")
            existing = conn.execute(
                "SELECT * FROM orders WHERE account_id = %s AND purchase_id = %s",
                (payload["account_id"], payload["purchase_id"]),
            ).fetchone()
            if existing and any(existing[k] != v for k, v in payload.items()):
                raise Rejection("purchase_conflict")
        if existing:
            result = existing
        else:
            product = conn.execute(
                "SELECT * FROM products WHERE sku = %s FOR UPDATE", (payload["sku"],)
            ).fetchone()
            if product is None:
                raise Rejection("invalid_request", 422)
            amount = product["price_minor"] * payload["quantity"]
            if account["balance_minor"] < amount:
                raise Rejection("insufficient_funds")
            if product["stock"] < payload["quantity"]:
                raise Rejection("insufficient_stock")
            result = dict(
                payload,
                order_id=str(uuid4()),
                amount_minor=amount,
                currency="INR",
                status="confirmed",
            )
            conn.execute(
                "INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(
                    result[k]
                    for k in (
                        "order_id",
                        "purchase_id",
                        "account_id",
                        "sku",
                        "quantity",
                        "amount_minor",
                        "currency",
                        "status",
                    )
                ),
            )
            conn.execute(
                "UPDATE accounts SET balance_minor = balance_minor - %s WHERE account_id = %s",
                (amount, payload["account_id"]),
            )
            conn.execute(
                "UPDATE products SET stock = stock - %s WHERE sku = %s",
                (payload["quantity"], payload["sku"]),
            )
            conn.execute(
                "INSERT INTO ledger VALUES (%s,%s,%s,%s,'debit',%s,'INR')",
                (
                    str(uuid4()),
                    payload["account_id"],
                    payload["purchase_id"],
                    result["order_id"],
                    amount,
                ),
            )
            conn.execute(
                "INSERT INTO movements VALUES (%s,%s,%s,%s,%s)",
                (
                    str(uuid4()),
                    payload["sku"],
                    payload["purchase_id"],
                    result["order_id"],
                    -payload["quantity"],
                ),
            )
            conn.execute(
                "INSERT INTO outbox VALUES (%s,%s,%s,'order_confirmed',%s)",
                (str(uuid4()), payload["purchase_id"], result["order_id"], Jsonb(result)),
            )
        # The unsafe variant deliberately records each token without enforcing uniqueness.
        if (
            variant == "unsafe"
            or not conn.execute(
                "SELECT 1 FROM idempotency WHERE account_id = %s AND key = %s",
                (payload["account_id"], key),
            ).fetchone()
        ):
            conn.execute(
                "INSERT INTO idempotency VALUES (%s,%s,%s,%s,%s)",
                (str(uuid4()), payload["account_id"], key, fingerprint, result["order_id"]),
            )
    return result


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PROCESS_EPOCH = str(uuid4())


@app.exception_handler(psycopg.Error)
async def dependency_error(_request, _exc):
    # A transport failure says nothing about commit. The caller reuses its identity.
    return JSONResponse(
        status_code=503,
        content={"code": "dependency_unavailable", "message": "Database connection unavailable"},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _exc):
    return JSONResponse(
        status_code=422, content={"code": "invalid_request", "message": "Invalid input"}
    )


@app.get("/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1 FROM accounts LIMIT 1")
    return {"ready": True}


@app.post("/orders", status_code=201)
def create_order(
    body: OrderRequest, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]
):
    try:
        # Registration commits before any business mutation. The separate session stays
        # open until the handler can no longer mutate, including after client timeout.
        with connect(autocommit=True) as registry:
            registration = str(uuid4())
            registry.execute(
                "INSERT INTO inflight SELECT %s, %s, pid, backend_start FROM pg_stat_activity "
                "WHERE pid = pg_backend_pid()",
                (registration, PROCESS_EPOCH),
            )
            try:
                delay = int(os.environ.get("FL_TEST_HANDLER_DELAY_MS", "0"))
                if not 0 <= delay <= 30000:
                    raise ValueError("invalid test handler delay")
                time.sleep(delay / 1000)
                return transact_order(body.model_dump(), idempotency_key, os.environ["FL_VARIANT"])
            finally:
                try:
                    registry.execute(
                        "DELETE FROM inflight WHERE registration_id = %s", (registration,)
                    )
                except psycopg.Error:
                    # A broken registry connection cannot establish completion. Leave the
                    # durable row for post-exit session reconciliation by the observer.
                    pass
    except Rejection as exc:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.code})
