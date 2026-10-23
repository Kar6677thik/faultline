from copy import deepcopy

import pytest
from conftest import envelope

from faultline.models import verdict
from faultline.verifier import EvidenceError, validate_baseline, verify


def statuses(results):
    return {r["invariant_id"]: r["status"] for r in results}


def test_success_and_lost_final_response_are_equivalent(scenario, baseline, correct_snapshot):
    witness = {
        "outcome": "http_response",
        "status": 201,
        "body": correct_snapshot["records"]["orders"][0],
    }
    for outcome in ("http_response", "timeout"):
        results = verify(
            scenario, baseline, correct_snapshot, [{"outcome": outcome, "upstream": witness}]
        )
        assert set(statuses(results).values()) == {"PASS"}


def test_duplicate_effects_fail_even_when_balances_reconcile(scenario, baseline, correct_snapshot):
    state = correct_snapshot["records"]
    for name in ("orders", "ledger", "movements", "outbox", "idempotency"):
        duplicate = deepcopy(state[name][0])
        primary_key = next(iter(duplicate))
        duplicate[primary_key] += "-copy"
        duplicate["order_id"] = "order-1-copy"
        if name == "outbox":
            duplicate["payload"]["order_id"] = "order-1-copy"
        state[name].append(duplicate)
    state["accounts"][0]["balance_minor"] = 600000
    state["products"][0]["stock"] = 8
    results = verify(scenario, baseline, envelope(state), [])
    by_id = statuses(results)
    assert by_id["orders.at_most_one_debit"] == "FAIL"
    assert by_id["orders.wallet_conservation"] == "PASS"
    finding = results[0]["failures"][0]
    assert finding["observed"]["total_minor"] == 400000
    assert finding["expected"]["amount_minor"] == 200000


@pytest.mark.parametrize(
    ("mutate", "invariant"),
    [
        (lambda s: s["orders"].clear(), "orders.purchase_atomicity"),
        (lambda s: s["outbox"].clear(), "orders.confirmation_intent"),
        (lambda s: s["accounts"][0].update(balance_minor=1000000), "orders.wallet_conservation"),
        (lambda s: s["products"][0].update(stock=10), "orders.inventory_conservation"),
        (lambda s: s["ledger"][0].update(account_id="unknown"), "orders.purchase_atomicity"),
        (lambda s: s["idempotency"][0].update(fingerprint="wrong"), "orders.idempotency_contract"),
    ],
)
def test_independent_corruption_cases(scenario, baseline, correct_snapshot, mutate, invariant):
    mutate(correct_snapshot["records"])
    results = verify(scenario, baseline, envelope(correct_snapshot["records"]), [])
    assert statuses(results)[invariant] == "FAIL"


def test_backend_that_does_nothing_cannot_pass(scenario, baseline):
    assert statuses(verify(scenario, baseline, baseline, []))["orders.completed_purchase"] == "FAIL"


def test_unfinished_work_is_not_a_false_completion_failure(scenario, baseline):
    results = verify(scenario, baseline, baseline, [], workload_complete=False)
    assert statuses(results)["orders.completed_purchase"] == "UNKNOWN"
    assert verdict(results, [], False) == "INCONCLUSIVE"


def test_success_response_for_nonexistent_order_fails(scenario, baseline, correct_snapshot):
    forged = dict(correct_snapshot["records"]["orders"][0], order_id="made-up")
    result = verify(
        scenario,
        baseline,
        correct_snapshot,
        [{"upstream": {"outcome": "http_response", "status": 201, "body": forged}}],
    )
    assert statuses(result)["orders.idempotency_contract"] == "FAIL"


def test_retry_rejection_is_not_hidden_by_one_committed_order(scenario, baseline, correct_snapshot):
    witnesses = [
        {
            "upstream": {
                "outcome": "http_response",
                "status": 201,
                "body": correct_snapshot["records"]["orders"][0],
            }
        },
        {
            "upstream": {
                "outcome": "http_response",
                "status": 409,
                "body": {"code": "insufficient_funds", "message": "insufficient_funds"},
            }
        },
    ]
    results = verify(scenario, baseline, correct_snapshot, witnesses)
    assert statuses(results)["orders.idempotency_contract"] == "FAIL"
    assert statuses(results)["orders.at_most_one_debit"] == "PASS"


@pytest.mark.parametrize("flag", ["complete", "quiescent"])
def test_missing_authority_never_passes(scenario, baseline, correct_snapshot, flag):
    correct_snapshot[flag] = False
    results = verify(scenario, baseline, correct_snapshot, [])
    assert set(statuses(results).values()) == {"UNKNOWN"}
    assert verdict(results, [], True) == "INCONCLUSIVE"


def test_corrupt_hash_and_bad_fixture_are_errors(scenario, baseline, correct_snapshot):
    correct_snapshot["content_hash"] = "bad"
    with pytest.raises(EvidenceError, match="hash"):
        verify(scenario, baseline, correct_snapshot, [])
    baseline["records"]["accounts"][0]["balance_minor"] = 7
    with pytest.raises(EvidenceError, match="opening wallet"):
        validate_baseline(envelope(baseline["records"]))


@pytest.mark.parametrize(
    ("findings", "diagnostics", "coverage", "expected"),
    [
        ([{"status": "FAIL"}], [{"severity": "error"}], True, "ERROR"),
        ([{"status": "FAIL"}, {"status": "UNKNOWN"}], [], False, "FAIL"),
        ([{"status": "PASS"}], [], False, "INCONCLUSIVE"),
        ([], [], True, "INCONCLUSIVE"),
        ([{"status": "PASS"}], [], True, "PASS"),
    ],
)
def test_verdict_precedence(findings, diagnostics, coverage, expected):
    assert verdict(findings, diagnostics, coverage) == expected
