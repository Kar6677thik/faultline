"""Second reference domain: moving simulated funds between two accounts.

Shares no schema or transaction code with the order application. `correct` moves the
money in one transaction with both account rows locked in a fixed order. `non_atomic`
commits the debit, then the credit in a second transaction; with instrumentation
enabled it pauses at the `transfer.debit_committed` checkpoint so a controlled crash
can land exactly between the two commits.
"""

import os
import threading
import time
from typing import Annotated, Literal
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from faultline.models import digest

VARIANTS = ("correct", "non_atomic")
CHECKPOINT = "transfer.debit_committed"
# A paused handler resumes on its own after this long; the crash normally comes first.
CHECKPOINT_PAUSE_SECONDS = 5.0


def connect():
    return psycopg.connect(
        host=os.environ.get("FL_DB_HOST", "database"),
        port=int(os.environ.get("FL_DB_PORT", "5432")),
        dbname="faultline",
        user="fl_app",
        password=os.environ["FL_APP_PASSWORD"],
        connect_timeout=3,
        application_name="faultline_transfers",
        row_factory=dict_row,
        options="-c statement_timeout=5000 -c lock_timeout=5000",
    )


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transfer_id: Annotated[str, Field(min_length=1, max_length=64)]
    from_account_id: Annotated[str, Field(min_length=1, max_length=64)]
    to_account_id: Annotated[str, Field(min_length=1, max_length=64)]
    amount_minor: Annotated[int, Field(ge=1, le=9223372036854775807)]
    currency: Literal["INR"]


class Rejection(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status


RESULT = ("transfer_id", "from_account_id", "to_account_id", "amount_minor", "currency", "status")


class Checkpoints:
    """Test-only record of reached checkpoints, exposed to the worker on the client network."""

    def __init__(self):
        self.enabled = set(filter(None, os.environ.get("FL_CHECKPOINTS", "").split(",")))
        self.reached, self.lock = [], threading.Lock()

    def reach(self, name: str, attempt_id: str | None, transfer_id: str):
        if name not in self.enabled or attempt_id is None:
            return
        with self.lock:
            self.reached.append(
                {"checkpoint": name, "attempt_id": attempt_id, "transfer_id": transfer_id}
            )
        # The debit is durable; hold the handler so the coordinator can crash the process.
        time.sleep(CHECKPOINT_PAUSE_SECONDS)


CHECKPOINTS = Checkpoints()


def _existing(conn, body: dict, key: str):
    """Replay or conflict handling shared by both variants (identity, not atomicity)."""
    token = conn.execute(
        "SELECT * FROM idempotency WHERE account_id = %s AND key = %s",
        (body["from_account_id"], key),
    ).fetchone()
    if token and token["fingerprint"] != digest(body):
        raise Rejection("idempotency_conflict")
    transfer = conn.execute(
        "SELECT * FROM transfers WHERE transfer_id = %s", (body["transfer_id"],)
    ).fetchone()
    if transfer and any(transfer[k] != v for k, v in body.items()):
        raise Rejection("transfer_conflict")
    return transfer


def execute_transfer(body: dict, key: str, variant: str, attempt_id: str | None) -> dict:
    if variant not in VARIANTS:
        raise ValueError("unsupported variant")
    if body["from_account_id"] == body["to_account_id"]:
        raise Rejection("invalid_request", 422)
    with connect() as conn:
        # Lock both accounts in a fixed order so opposite transfers cannot deadlock.
        accounts = {
            row["account_id"]: row
            for row in conn.execute(
                "SELECT * FROM accounts WHERE account_id = ANY(%s) ORDER BY account_id FOR UPDATE",
                ([body["from_account_id"], body["to_account_id"]],),
            ).fetchall()
        }
        if len(accounts) != 2:
            raise Rejection("invalid_request", 422)
        existing = _existing(conn, body, key)
        if existing:
            _token(conn, body, key)
            return {k: existing[k] for k in RESULT}
        if accounts[body["from_account_id"]]["balance_minor"] < body["amount_minor"]:
            raise Rejection("insufficient_funds")
        result = {**body, "status": "committed"}
        conn.execute(
            "INSERT INTO transfers VALUES (%s,%s,%s,%s,%s,%s)", tuple(result[k] for k in RESULT)
        )
        conn.execute(
            "UPDATE accounts SET balance_minor = balance_minor - %s WHERE account_id = %s",
            (body["amount_minor"], body["from_account_id"]),
        )
        conn.execute(
            "INSERT INTO ledger VALUES (%s,%s,%s,'debit',%s,'INR')",
            (str(uuid4()), body["from_account_id"], body["transfer_id"], body["amount_minor"]),
        )
        _token(conn, body, key)
        if variant == "non_atomic":
            # The defect: the debit is durable before the credit exists.
            conn.commit()
            CHECKPOINTS.reach(CHECKPOINT, attempt_id, body["transfer_id"])
        conn.execute(
            "UPDATE accounts SET balance_minor = balance_minor + %s WHERE account_id = %s",
            (body["amount_minor"], body["to_account_id"]),
        )
        conn.execute(
            "INSERT INTO ledger VALUES (%s,%s,%s,'credit',%s,'INR')",
            (str(uuid4()), body["to_account_id"], body["transfer_id"], body["amount_minor"]),
        )
    return result


def _token(conn, body: dict, key: str):
    if not conn.execute(
        "SELECT 1 FROM idempotency WHERE account_id = %s AND key = %s",
        (body["from_account_id"], key),
    ).fetchone():
        conn.execute(
            "INSERT INTO idempotency VALUES (%s,%s,%s,%s,%s)",
            (str(uuid4()), body["from_account_id"], key, digest(body), body["transfer_id"]),
        )


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(psycopg.Error)
async def dependency_error(_request, _exc):
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


@app.get("/_faultline/checkpoints")
def checkpoints():
    # Test-only instrumentation; read by the worker, never by the business client.
    with CHECKPOINTS.lock:
        return {"checkpoints": list(CHECKPOINTS.reached)}


@app.post("/transfers", status_code=201)
def create_transfer(
    body: TransferRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    try:
        return execute_transfer(
            body.model_dump(),
            idempotency_key,
            os.environ["FL_VARIANT"],
            request.headers.get("x-faultline-attempt-id"),
        )
    except Rejection as exc:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.code})
