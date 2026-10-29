"""Fixture setup and read-only snapshots for the transfer domain (observer helper)."""

import json
import os
import sys
import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from faultline.models import digest

TABLES = ("accounts", "transfers", "ledger", "idempotency")
SCHEMA = """
CREATE TABLE accounts (account_id text PRIMARY KEY, balance_minor bigint NOT NULL);
CREATE TABLE transfers (
    transfer_id text NOT NULL, from_account_id text NOT NULL, to_account_id text NOT NULL,
    amount_minor bigint NOT NULL, currency text NOT NULL, status text NOT NULL
);
CREATE TABLE ledger (
    entry_id text PRIMARY KEY, account_id text NOT NULL, transfer_id text NOT NULL,
    direction text NOT NULL, amount_minor bigint NOT NULL, currency text NOT NULL
);
CREATE TABLE idempotency (
    record_id text PRIMARY KEY, account_id text NOT NULL, key text NOT NULL,
    fingerprint text NOT NULL, transfer_id text NOT NULL
);
"""
SEEDS = {"two_accounts": [("acct-a", 1000000), ("acct-b", 1000000)]}


def connect(role: str):
    return psycopg.connect(
        host="database",
        port=5432,
        dbname="faultline",
        user=f"fl_{role}",
        password=os.environ[f"FL_{role.upper()}_PASSWORD"],
        connect_timeout=3,
        application_name=f"faultline_{role}",
        row_factory=dict_row,
        options="-c statement_timeout=5000",
    )


def prepare(variant: str, fixture: str) -> dict:
    if variant not in {"correct", "non_atomic"} or fixture not in SEEDS:
        raise ValueError("unsupported variant or fixture")
    with connect("setup") as conn:
        conn.execute(SCHEMA)
        # Both variants keep identity unique; the defect under test is atomicity.
        conn.execute("CREATE UNIQUE INDEX transfer_identity ON transfers(transfer_id)")
        conn.execute("CREATE UNIQUE INDEX transfer_key ON idempotency(account_id, key)")
        for account in SEEDS[fixture]:
            conn.execute("INSERT INTO accounts VALUES (%s, %s)", account)
        for role in ("app", "observer"):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(f"fl_{role}"),
                    sql.Literal(os.environ[f"FL_{role.upper()}_PASSWORD"]),
                )
            )
        conn.execute("GRANT USAGE ON SCHEMA public TO fl_app, fl_observer")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO fl_app")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO fl_observer")
        conn.execute("GRANT pg_read_all_stats TO fl_observer")
        conn.execute("ALTER ROLE fl_observer SET default_transaction_read_only = on")
    return {"prepared": True, "fixture": fixture}


def snapshot(role: str) -> dict:
    started = time.time_ns()
    with connect("observer") as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        records = {
            table: conn.execute(
                sql.SQL("SELECT * FROM {} ORDER BY 1, 2").format(sql.Identifier(table))
            ).fetchall()
            for table in TABLES
        }
        transaction = conn.execute("SELECT pg_current_snapshot()::text AS id").fetchone()["id"]
        sessions = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = 'fl_app'"
        ).fetchone()["n"]
    return {
        "snapshot_schema_version": 1,
        "snapshot_id": role,
        "role": role,
        "adapter_profile": "transfers_v1",
        "fixture": "two_accounts",
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


if __name__ == "__main__":
    command, argument = sys.argv[1:]
    if command == "prepare":
        selection = json.loads(argument)
        result = prepare(selection["variant"], selection["fixture"])
    elif command == "snapshot":
        result = snapshot(argument)
    else:
        raise SystemExit("unknown transfer helper command")
    print(json.dumps(result, separators=(",", ":")))
