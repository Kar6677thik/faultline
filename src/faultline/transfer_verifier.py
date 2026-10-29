"""Pure verification of the transfer domain from independently observed records.

Knows nothing about the transfer application's transactions or which variant ran.
"""

from collections import Counter

from faultline.models import digest
from faultline.verifier import EvidenceError

INVARIANTS = (
    "transfers.conservation",
    "transfers.atomicity",
    "transfers.idempotency_contract",
    "transfers.completed_transfer",
)
FIELDS = {
    "accounts": {"account_id": str, "balance_minor": int},
    "transfers": {
        "transfer_id": str,
        "from_account_id": str,
        "to_account_id": str,
        "amount_minor": int,
        "currency": str,
        "status": str,
    },
    "ledger": {
        "entry_id": str,
        "account_id": str,
        "transfer_id": str,
        "direction": str,
        "amount_minor": int,
        "currency": str,
    },
    "idempotency": {
        "record_id": str,
        "account_id": str,
        "key": str,
        "fingerprint": str,
        "transfer_id": str,
    },
}
# Written independently of the application's seed list.
OPENING = {
    "two_accounts": [
        {"account_id": "acct-a", "balance_minor": 1000000},
        {"account_id": "acct-b", "balance_minor": 1000000},
    ]
}
REJECTIONS = {
    "insufficient_funds": (409, "insufficient_funds"),
    "idempotency_conflict": (409, "idempotency_conflict"),
    "transfer_conflict": (409, "transfer_conflict"),
    "invalid_request": (422, "invalid_request"),
}
RETRYABLE = {429, 500, 502, 503, 504}


def fixture_records(name: str) -> dict:
    if name not in OPENING:
        raise EvidenceError("unregistered transfer fixture")
    return {
        table: [dict(r) for r in OPENING[name]] if table == "accounts" else [] for table in FIELDS
    }


def validate_snapshot(snapshot: dict) -> None:
    records = snapshot.get("records")
    if not isinstance(records, dict) or set(records) != set(FIELDS):
        raise EvidenceError("missing or unexpected transfer record sets")
    for table, fields in FIELDS.items():
        for row in records[table]:
            if not isinstance(row, dict) or set(row) != set(fields):
                raise EvidenceError(f"invalid record fields: {table}")
            if any(type(row[key]) is not kind for key, kind in fields.items()):
                raise EvidenceError(f"invalid record types: {table}")


def validate_baseline(snapshot: dict) -> None:
    validate_snapshot(snapshot)
    if not snapshot["complete"] or not snapshot["quiescent"]:
        raise EvidenceError("baseline is not complete and quiescent")
    if snapshot["records"] != fixture_records(snapshot.get("fixture")):
        raise EvidenceError("baseline differs from the opening transfer fixture")


def verify(scenario, baseline, final, attempts, *, workload_complete=True) -> list[dict]:
    validate_baseline(baseline)
    validate_snapshot(final)
    groups = {}
    for op in scenario.all_operations:
        groups.setdefault(op.logical_id, []).append(op)
    certain = final["complete"] and final["quiescent"]
    state = final["records"]
    opening = {r["account_id"]: r["balance_minor"] for r in baseline["records"]["accounts"]}
    conservation = _conservation(opening, state)
    results = []
    for logical_id, operations in groups.items():
        failures = {name: [] for name in INVARIANTS}
        if certain:
            failures["transfers.conservation"] = list(conservation)
            _per_transfer(logical_id, operations, state, attempts, workload_complete, failures)
        results.extend(_results(logical_id, failures, certain, workload_complete))
    known = set(groups)
    unknown = [
        row
        for table in ("transfers", "ledger")
        for row in state[table]
        if row["transfer_id"] not in known
    ]
    if unknown and certain:
        first = next(r for r in results if r["invariant_id"] == "transfers.atomicity")
        _add(first, "unrequested_effect", [], unknown, ["transfers", "ledger"])
    return results


def _conservation(opening: dict, state: dict) -> list[dict]:
    net = Counter()
    bad_rows = []
    for row in state["ledger"]:
        sign = {"debit": -1, "credit": 1}.get(row["direction"])
        if (
            sign is None
            or row["amount_minor"] <= 0
            or row["currency"] != "INR"
            or row["account_id"] not in opening
        ):
            bad_rows.append(row)
            continue
        net[row["account_id"]] += sign * row["amount_minor"]
    expected = [
        {"account_id": account, "balance_minor": balance + net[account]}
        for account, balance in opening.items()
    ]
    observed = state["accounts"]
    total = sum(r["balance_minor"] for r in observed)
    if (
        observed != expected
        or bad_rows
        or total != sum(opening.values())
        or any(r["balance_minor"] < 0 for r in observed)
    ):
        return [
            {
                "code": "balance_mismatch",
                "expected": {"accounts": expected, "total_minor": sum(opening.values())},
                "observed": {"accounts": observed, "total_minor": total, "invalid": bad_rows},
                "evidence": [
                    "snapshots/final.json#/records/accounts",
                    "snapshots/final.json#/records/ledger",
                ],
            }
        ]
    return []


def _per_transfer(logical_id, operations, state, attempts, workload_complete, failures):
    positive = [op for op in operations if op.expectation == "success"]
    intent = (positive or operations)[0].request.body.model_dump()
    transfers = [r for r in state["transfers"] if r["transfer_id"] == logical_id]
    ledger = [r for r in state["ledger"] if r["transfer_id"] == logical_id]
    debits = [r for r in ledger if r["direction"] == "debit"]
    credits = [r for r in ledger if r["direction"] == "credit"]
    matching = [
        r
        for r in transfers
        if all(r[k] == intent[k] for k in intent) and r["status"] == "committed"
    ]

    def fail(name, code, expected, observed, tables):
        failures[name].append(
            {
                "code": code,
                "expected": expected,
                "observed": observed,
                "evidence": [f"snapshots/final.json#/records/{t}" for t in tables],
            }
        )

    if positive:
        legs_ok = (
            len(debits) == len(credits) == len(matching) == len(transfers) <= 1
            and all(
                r["account_id"] == intent["from_account_id"]
                and r["amount_minor"] == intent["amount_minor"]
                and r["currency"] == "INR"
                for r in debits
            )
            and all(
                r["account_id"] == intent["to_account_id"]
                and r["amount_minor"] == intent["amount_minor"]
                and r["currency"] == "INR"
                for r in credits
            )
        )
        if not legs_ok:
            one_sided = len(debits) != len(credits)
            fail(
                "transfers.atomicity",
                "one_sided_transfer" if one_sided else "duplicate_or_mismatched_effect",
                "one transfer with one matching debit and one matching credit, or none",
                {
                    "transfers": len(transfers),
                    "debits": [r["entry_id"] for r in debits],
                    "credits": [r["entry_id"] for r in credits],
                },
                ["transfers", "ledger"],
            )
    elif transfers or ledger:
        fail(
            "transfers.atomicity",
            "rejected_transfer_has_effects",
            [],
            {"transfers": transfers, "ledger": ledger},
            ["transfers", "ledger"],
        )

    op_ids = {op.id: op for op in operations}
    keys = {
        next(v for k, v in op.request.headers.items() if k.lower() == "idempotency-key")
        for op in operations
    }
    tokens = [r for r in state["idempotency"] if r["transfer_id"] == logical_id]
    consistent = len({(r["account_id"], r["key"]) for r in tokens}) == len(tokens)
    consistent &= all(r["key"] in keys and r["fingerprint"] == digest(intent) for r in tokens)
    observed_rejections = set()
    for attempt in attempts:
        op = op_ids.get(attempt["attempt_id"].rsplit("/", 1)[0])
        witness = attempt.get("upstream") or {}
        if op is None or witness.get("outcome") != "http_response":
            continue
        status, body = witness.get("status"), witness.get("body")
        if op.expectation == "success":
            if status == 201:
                consistent &= body in [{k: r[k] for k in FIELDS["transfers"]} for r in matching]
            else:
                consistent &= status in RETRYABLE
        else:
            expected = REJECTIONS.get(op.expectation)
            ok = (
                expected is not None
                and status == expected[0]
                and isinstance(body, dict)
                and (body.get("code") == expected[1])
            )
            if ok:
                observed_rejections.add(op.id)
            consistent &= ok or status in RETRYABLE
    if not consistent:
        fail(
            "transfers.idempotency_contract",
            "inconsistent_transfer_result",
            "responses match the single stored transfer; conflicts rejected",
            {"idempotency": tokens},
            ["idempotency", "transfers"],
        )
    rejected = {op.id for op in operations if op.expectation != "success"}
    if workload_complete and (
        len(matching) != (1 if positive else 0) or rejected != observed_rejections
    ):
        fail(
            "transfers.completed_transfer",
            "transfer_not_completed_once",
            1 if positive else 0,
            len(matching),
            ["transfers"],
        )


def _signature(name, code, logical_id):
    return digest(
        {
            "invariant": name,
            "version": 1,
            "code": code,
            "logical_id": logical_id,
            "adapter": "transfers_v1",
        }
    )


def _results(logical_id, failures, certain, workload_complete):
    results = []
    for name in INVARIANTS:
        result = {
            "invariant_id": name,
            "version": 1,
            "logical_id": logical_id,
            "evidence": ["snapshots/baseline.json", "snapshots/final.json"],
            "failures": failures[name],
            "signatures": [_signature(name, f["code"], logical_id) for f in failures[name]],
        }
        if not certain:
            result.update(status="UNKNOWN", reason="not_quiescent_or_incomplete")
        elif name == "transfers.completed_transfer" and not workload_complete:
            result.update(status="UNKNOWN", reason="workload_incomplete")
        else:
            result["status"] = "FAIL" if failures[name] else "PASS"
        results.append(result)
    return results


def _add(result, code, expected, observed, tables):
    result["status"] = "FAIL"
    result["failures"].append(
        {
            "code": code,
            "expected": expected,
            "observed": observed,
            "evidence": [f"snapshots/final.json#/records/{t}" for t in tables],
        }
    )
    result["signatures"].append(_signature(result["invariant_id"], code, result["logical_id"]))
