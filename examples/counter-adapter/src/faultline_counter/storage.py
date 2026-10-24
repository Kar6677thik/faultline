"""Fixture/snapshot helper, kept separate from the HTTP transaction implementation."""

import json
import os
import sys
import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from faultline.models import digest


def connect(role):
    return psycopg.connect(
        host="proxy" if role == "app" else "database",
        port=8666 if role == "app" else 5432,
        dbname="faultline",
        user=f"fl_{role}",
        password=os.environ[f"FL_{role.upper()}_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=3,
        options="-c statement_timeout=5000",
    )


def prepare():
    with connect("setup") as conn:
        conn.execute(
            "CREATE TABLE counter (total bigint NOT NULL); INSERT INTO counter VALUES (0); "
            "CREATE TABLE increments (entry_id text PRIMARY KEY, intent text NOT NULL, "
            "delta bigint NOT NULL)"
        )
        for role in ("app", "observer"):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(f"fl_{role}"),
                    sql.Literal(os.environ[f"FL_{role.upper()}_PASSWORD"]),
                )
            )
        conn.execute("GRANT USAGE ON SCHEMA public TO fl_app,fl_observer")
        conn.execute("GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO fl_app")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO fl_observer")
        conn.execute("GRANT pg_read_all_stats TO fl_observer")
        conn.execute("ALTER ROLE fl_observer SET default_transaction_read_only=on")
    return {"prepared": True}


def snapshot(role):
    started = time.time_ns()
    with connect("observer") as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        records = {
            "counter": conn.execute("SELECT * FROM counter").fetchall(),
            "increments": conn.execute("SELECT * FROM increments ORDER BY entry_id").fetchall(),
        }
        sessions = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename='fl_app'"
        ).fetchone()["n"]
        transaction = conn.execute("SELECT pg_current_snapshot()::text AS id").fetchone()["id"]
    return {
        "snapshot_schema_version": 1,
        "snapshot_id": role,
        "role": role,
        "authority": "postgresql",
        "adapter_profile": "counter_v1",
        "fixture": "empty_counter",
        "complete": True,
        "quiescent": sessions == 0,
        "application_sessions": sessions,
        "records": records,
        "row_counts": {k: len(v) for k, v in records.items()},
        "content_hash": digest(records),
        "isolation": "repeatable_read_read_only",
        "started_ns": started,
        "completed_ns": time.time_ns(),
        "transaction": transaction,
    }


if __name__ == "__main__":
    command, argument = sys.argv[1:]
    result = prepare() if command == "prepare" else snapshot(argument)
    print(json.dumps(result))
