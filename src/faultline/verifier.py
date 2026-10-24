"""Pure verification of complete, independently observed order records."""

from collections import Counter

from faultline.models import INVARIANTS, Scenario, digest

FIELDS = {
    "accounts": {"account_id": str, "balance_minor": int},
    "products": {"sku": str, "price_minor": int, "stock": int},
    "orders": {
        "order_id": str,
        "purchase_id": str,
        "account_id": str,
        "sku": str,
        "quantity": int,
        "amount_minor": int,
        "currency": str,
        "status": str,
    },
    "ledger": {
        "entry_id": str,
        "account_id": str,
        "purchase_id": str,
        "order_id": str,
        "direction": str,
        "amount_minor": int,
        "currency": str,
    },
    "movements": {
        "movement_id": str,
        "sku": str,
        "purchase_id": str,
        "order_id": str,
        "quantity_delta": int,
    },
    "outbox": {
        "event_id": str,
        "purchase_id": str,
        "order_id": str,
        "event_type": str,
        "payload": dict,
    },
    "idempotency": {
        "record_id": str,
        "account_id": str,
        "key": str,
        "fingerprint": str,
        "order_id": str,
    },
}


class EvidenceError(ValueError):
    pass


def validate_snapshot(snapshot: dict) -> None:
    if snapshot.get("snapshot_schema_version") != 1:
        raise EvidenceError("unsupported snapshot schema")
    records = snapshot.get("records")
    if not isinstance(records, dict) or set(records) != set(FIELDS):
        raise EvidenceError("missing or unexpected record sets")
    if type(snapshot.get("complete")) is not bool or type(snapshot.get("quiescent")) is not bool:
        raise EvidenceError("missing completeness or quiescence metadata")
    if snapshot.get("authority") != "postgresql":
        raise EvidenceError("unsupported authority")
    if snapshot.get("content_hash") != digest(records):
        raise EvidenceError("snapshot hash mismatch")
    for table, fields in FIELDS.items():
        rows = records[table]
        if not isinstance(rows, list) or snapshot.get("row_counts", {}).get(table) != len(rows):
            raise EvidenceError(f"invalid row counts: {table}")
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(fields):
                raise EvidenceError(f"invalid record fields: {table}")
            if any(type(row[key]) is not kind for key, kind in fields.items()):
                raise EvidenceError(f"invalid record types: {table}")
            row_id = row[next(iter(fields))]
            if row_id in ids:
                raise EvidenceError(f"duplicate source primary key: {table}")
            ids.add(row_id)


def validate_baseline(snapshot: dict) -> None:
    validate_snapshot(snapshot)
    records = snapshot["records"]
    if not snapshot["complete"] or not snapshot["quiescent"]:
        raise EvidenceError("baseline is not complete and quiescent")
    if records["accounts"] != [{"account_id": "buyer-a", "balance_minor": 1000000}]:
        raise EvidenceError("incorrect opening wallet")
    if records["products"] != [{"sku": "sku-a", "price_minor": 200000, "stock": 10}]:
        raise EvidenceError("incorrect opening inventory")
    if any(records[name] for name in FIELDS if name not in {"accounts", "products"}):
        raise EvidenceError("fixture has preexisting business effects")


def verify(
    scenario: Scenario,
    baseline: dict,
    final: dict,
    attempts: list[dict],
    *,
    workload_complete: bool = True,
) -> list[dict]:
    validate_baseline(baseline)
    validate_snapshot(final)
    results = []
    groups = {}
    for operation in scenario.all_operations:
        groups.setdefault(operation.logical_id, []).append(operation)
    allowed_attempts = {
        f"{op.id}/{i}"
        for op in scenario.all_operations
        for i in range(1, op.retry.max_attempts + 1)
    }
    seen = set()
    for attempt in attempts:
        identity = attempt.get("attempt_id")
        if identity not in allowed_attempts or identity in seen:
            raise EvidenceError("unknown or duplicate attempt identity")
        seen.add(identity)
    for operations in groups.values():
        op_ids = {op.id for op in operations}
        selected = [a for a in attempts if a["attempt_id"].rsplit("/", 1)[0] in op_ids]
        results.extend(_verify_purchase(operations, baseline, final, selected, workload_complete))
    # Filtering by declared identity must never silently discard orphan/unrequested effects.
    unknown = [
        row
        for table in ("orders", "ledger", "movements", "outbox")
        for row in final["records"][table]
        if row["purchase_id"] not in groups
    ]
    if unknown and final["complete"] and final["quiescent"]:
        result = next(r for r in results if r["invariant_id"] == "orders.purchase_atomicity")
        result["status"] = "FAIL"
        result["failures"].append(
            {
                "code": "unrequested_effect",
                "expected": [],
                "observed": unknown,
                "evidence": ["snapshots/final.json#/records"],
            }
        )
        result["signatures"].append(digest(["orders.purchase_atomicity", "unrequested_effect"]))
    order_ids = {row["order_id"] for row in final["records"]["orders"]}
    orphan_tokens = [
        row for row in final["records"]["idempotency"] if row["order_id"] not in order_ids
    ]
    if orphan_tokens and final["complete"] and final["quiescent"]:
        result = next(r for r in results if r["invariant_id"] == "orders.idempotency_contract")
        result["status"] = "FAIL"
        result["failures"].append(
            {
                "code": "orphan_idempotency_record",
                "expected": [],
                "observed": orphan_tokens,
                "evidence": ["snapshots/final.json#/records/idempotency"],
            }
        )
        result["signatures"].append(
            digest(["orders.idempotency_contract", "orphan_idempotency_record"])
        )
    return results


def _verify_purchase(operations, baseline, final, attempts, workload_complete):
    positive = [op for op in operations if op.expectation == "success"]
    operation = (positive or operations)[0]
    intent = operation.request.body.model_dump()
    logical_id = operation.logical_id
    if not final["complete"] or not final["quiescent"]:
        return [
            {
                "invariant_id": name,
                "version": 1,
                "status": "UNKNOWN",
                "logical_id": logical_id,
                "failures": [],
                "reason": "not_quiescent" if not final["quiescent"] else "snapshot_incomplete",
                "evidence": ["snapshots/final.json"],
            }
            for name in INVARIANTS
        ]

    state = final["records"]
    orders, ledger, movements, outbox = (
        [row for row in state[k] if row["purchase_id"] == logical_id]
        for k in ("orders", "ledger", "movements", "outbox")
    )
    amount = baseline["records"]["products"][0]["price_minor"] * intent["quantity"]
    failures = {name: [] for name in INVARIANTS}

    def fail(invariant, code, expected, observed, tables):
        failures["orders." + invariant].append(
            {
                "code": code,
                "expected": expected,
                "observed": observed,
                "evidence": [f"snapshots/final.json#/records/{table}" for table in tables],
            }
        )

    debits = [
        row
        for row in ledger
        if row["direction"] == "debit"
        and row["account_id"] == intent["account_id"]
        and row["purchase_id"] == logical_id
    ]
    if len(debits) > 1 or any(row["amount_minor"] != amount for row in debits):
        fail(
            "at_most_one_debit",
            "duplicate_debit" if len(debits) > 1 else "wrong_amount",
            {"maximum_count": 1, "amount_minor": amount},
            {
                "count": len(debits),
                "total_minor": sum(r["amount_minor"] for r in debits),
                "entry_ids": [r["entry_id"] for r in debits],
            },
            ["ledger"],
        )

    valid_orders = [
        row
        for row in orders
        if all(row[k] == v for k, v in intent.items())
        and row["amount_minor"] == amount
        and row["currency"] == "INR"
        and row["status"] == "confirmed"
    ]
    ids = {row["order_id"] for row in valid_orders}
    atomic = len(orders) <= 1 and len(valid_orders) == len(orders)
    atomic &= len(ledger) == len(orders) == len(movements)
    for row in ledger:
        atomic &= (
            row["order_id"] in ids
            and row["account_id"] == intent["account_id"]
            and row["purchase_id"] == logical_id
            and row["direction"] == "debit"
            and row["currency"] == "INR"
            and row["amount_minor"] == amount
        )
    for row in movements:
        atomic &= (
            row["order_id"] in ids
            and row["purchase_id"] == logical_id
            and row["sku"] == intent["sku"]
            and row["quantity_delta"] == -intent["quantity"]
        )
    if not atomic:
        fail(
            "purchase_atomicity",
            "non_atomic_purchase",
            "one matching order/debit/movement or none",
            {"orders": len(orders), "debits": len(ledger), "movements": len(movements)},
            ["orders", "ledger", "movements"],
        )

    expected_balance = 1000000 - sum(
        row["amount_minor"] * (1 if row["direction"] == "debit" else -1) for row in state["ledger"]
    )
    balances = state["accounts"]
    if (
        balances != [{"account_id": "buyer-a", "balance_minor": expected_balance}]
        or expected_balance < 0
        or any(
            r["account_id"] != "buyer-a"
            or r["direction"] not in {"debit", "credit"}
            or r["amount_minor"] <= 0
            or r["currency"] != "INR"
            for r in state["ledger"]
        )
    ):
        fail(
            "wallet_conservation",
            "balance_mismatch",
            expected_balance,
            balances,
            ["accounts", "ledger"],
        )

    expected_stock = 10 + sum(row["quantity_delta"] for row in state["movements"])
    if (
        state["products"] != [{"sku": "sku-a", "price_minor": 200000, "stock": expected_stock}]
        or expected_stock < 0
        or any(row["sku"] != "sku-a" for row in state["movements"])
    ):
        fail(
            "inventory_conservation",
            "stock_mismatch",
            expected_stock,
            state["products"],
            ["products", "movements"],
        )

    orders_by_id = {row["order_id"]: row for row in orders}
    if Counter(r["order_id"] for r in outbox) != Counter(orders_by_id.keys()) or any(
        row["order_id"] not in orders_by_id
        or row["purchase_id"] != logical_id
        or row["event_type"] != "order_confirmed"
        or row["payload"] != orders_by_id.get(row["order_id"])
        for row in outbox
    ):
        fail(
            "confirmation_intent",
            "outbox_mismatch",
            "one matching intent per order",
            outbox,
            ["outbox", "orders"],
        )

    witnesses = [
        a["upstream"] for a in attempts if a.get("upstream", {}).get("outcome") == "http_response"
    ]
    success = [w for w in witnesses if w["status"] == 201]
    keys = {
        next(v for k, v in op.request.headers.items() if k.lower() == "idempotency-key")
        for op in positive
    }
    tokens = [row for row in state["idempotency"] if row["key"] in keys or row["order_id"] in ids]
    idempotent = all(w["body"] in valid_orders for w in success) and (bool(positive) or not success)
    # This profile declares only positive purchases. A retry that returns (for example)
    # insufficient_funds after the first committed debit is not an idempotent API result,
    # even if the depleted wallet incidentally prevents a second debit.
    expected_rejections = {
        op.id: (422, "invalid_request")
        if op.expectation == "invalid_quantity"
        else (409, op.expectation)
        for op in operations
        if op.expectation != "success"
    }
    observed_rejections = set()
    for attempt in attempts:
        witness = attempt.get("upstream", {})
        if witness.get("outcome") != "http_response":
            continue
        op_id = attempt["attempt_id"].rsplit("/", 1)[0]
        if op_id in expected_rejections:
            status, code = expected_rejections[op_id]
            matches = (
                witness.get("status") == status
                and isinstance(witness.get("body"), dict)
                and witness["body"].get("code") == code
            )
            if matches:
                observed_rejections.add(op_id)
            idempotent &= matches or witness["status"] in {429, 500, 502, 503, 504}
        else:
            idempotent &= witness["status"] == 201 or witness["status"] in {429, 500, 502, 503, 504}
    idempotent &= len({digest(w["body"]) for w in success}) <= 1
    idempotent &= (bool(tokens) == bool(orders)) and len(tokens) <= len(keys)
    idempotent &= len({(r["account_id"], r["key"]) for r in tokens}) == len(tokens)
    idempotent &= all(
        row["order_id"] in ids
        and row["account_id"] == intent["account_id"]
        and row["key"] in keys
        and row["fingerprint"] == digest(intent)
        for row in tokens
    )
    if not idempotent:
        fail(
            "idempotency_contract",
            "inconsistent_purchase_result",
            "one stored and observed result",
            {"responses": success, "idempotency": tokens},
            ["idempotency", "orders"],
        )

    expected_count = 1 if positive else 0
    if workload_complete and (
        len(valid_orders) != expected_count or set(expected_rejections) != observed_rejections
    ):
        fail(
            "completed_purchase",
            "purchase_not_completed_once",
            expected_count,
            len(valid_orders),
            ["orders"],
        )
    if not positive and any((orders, ledger, movements, outbox)):
        fail(
            "purchase_atomicity",
            "rejected_purchase_has_effects",
            [],
            orders,
            ["orders", "ledger", "movements", "outbox"],
        )

    results = []
    for name in INVARIANTS:
        issues = failures[name]
        if name == "orders.completed_purchase" and not workload_complete:
            results.append(
                {
                    "invariant_id": name,
                    "version": 1,
                    "status": "UNKNOWN",
                    "logical_id": logical_id,
                    "failures": [],
                    "reason": "workload_incomplete",
                    "evidence": ["history.ndjson"],
                    "signatures": [],
                }
            )
            continue
        results.append(
            {
                "invariant_id": name,
                "version": 1,
                "status": "FAIL" if issues else "PASS",
                "logical_id": logical_id,
                "failures": issues,
                "evidence": ["snapshots/baseline.json", "snapshots/final.json"],
                "signatures": [
                    digest(
                        {
                            "invariant": name,
                            "version": 1,
                            "code": f["code"],
                            "logical_id": logical_id,
                            "adapter": "orders_v1",
                        }
                    )
                    for f in issues
                ],
            }
        )
    return results
