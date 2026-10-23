from copy import deepcopy
from pathlib import Path

import pytest

from faultline.models import digest, load_scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario():
    return load_scenario(ROOT / "examples/orders/lost-response.yaml")


def envelope(records, role="final"):
    return {
        "snapshot_schema_version": 1,
        "snapshot_id": role,
        "role": role,
        "authority": "postgresql",
        "complete": True,
        "quiescent": True,
        "records": records,
        "row_counts": {k: len(v) for k, v in records.items()},
        "content_hash": digest(records),
    }


@pytest.fixture
def baseline():
    # Independent hand-audited fixture; does not call the AUT or observation projection.
    return envelope(
        {
            "accounts": [{"account_id": "buyer-a", "balance_minor": 1000000}],
            "products": [{"sku": "sku-a", "price_minor": 200000, "stock": 10}],
            "orders": [],
            "ledger": [],
            "movements": [],
            "outbox": [],
            "idempotency": [],
        },
        "baseline",
    )


@pytest.fixture
def correct_snapshot(baseline):
    records = deepcopy(baseline["records"])
    order = {
        "order_id": "order-1",
        "purchase_id": "purchase-a",
        "account_id": "buyer-a",
        "sku": "sku-a",
        "quantity": 1,
        "amount_minor": 200000,
        "currency": "INR",
        "status": "confirmed",
    }
    records["accounts"][0]["balance_minor"] = 800000
    records["products"][0]["stock"] = 9
    records["orders"] = [order]
    records["ledger"] = [
        {
            "entry_id": "debit-1",
            "account_id": "buyer-a",
            "purchase_id": "purchase-a",
            "order_id": "order-1",
            "direction": "debit",
            "amount_minor": 200000,
            "currency": "INR",
        }
    ]
    records["movements"] = [
        {
            "movement_id": "movement-1",
            "sku": "sku-a",
            "purchase_id": "purchase-a",
            "order_id": "order-1",
            "quantity_delta": -1,
        }
    ]
    records["outbox"] = [
        {
            "event_id": "event-1",
            "purchase_id": "purchase-a",
            "order_id": "order-1",
            "event_type": "order_confirmed",
            "payload": order.copy(),
        }
    ]
    records["idempotency"] = [
        {
            "record_id": "key-1",
            "account_id": "buyer-a",
            "key": "purchase-a-key",
            "fingerprint": digest(
                {
                    "purchase_id": "purchase-a",
                    "account_id": "buyer-a",
                    "sku": "sku-a",
                    "quantity": 1,
                }
            ),
            "order_id": "order-1",
        }
    ]
    return envelope(records)
