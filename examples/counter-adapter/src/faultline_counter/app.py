"""Separately packaged counter application. No order schema or order transactions."""

import os
from uuid import uuid4

from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field

from .storage import connect

app = FastAPI()


class Increment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: str = Field(min_length=1, max_length=64)
    delta: int = Field(ge=1, le=100)


@app.get("/health")
def health():
    with connect("app") as conn:
        conn.execute("SELECT total FROM counter")
    return {"ready": True}


@app.post("/increments", status_code=201)
def increment(body: Increment, idempotency_key: str = Header()):
    with connect("app") as conn:
        conn.execute("SELECT total FROM counter FOR UPDATE")
        existing = conn.execute(
            "SELECT * FROM increments WHERE intent=%s", (body.intent,)
        ).fetchone()
        if os.environ["FL_VARIANT"] == "correct" and existing:
            return existing
        result = {"entry_id": str(uuid4()), "intent": body.intent, "delta": body.delta}
        conn.execute("INSERT INTO increments VALUES (%s,%s,%s)", tuple(result.values()))
        conn.execute("UPDATE counter SET total=total+%s", (body.delta,))
    return result
