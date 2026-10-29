import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from faultline import transfer_verifier
from faultline.adapter_loader import resolve_adapter
from faultline.faults import FaultController
from faultline.models import Scenario, digest, load_document
from faultline.projects import load_project
from faultline.verifier import EvidenceError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/transfers"
INTENT = {
    "transfer_id": "transfer-1",
    "from_account_id": "acct-a",
    "to_account_id": "acct-b",
    "amount_minor": 250000,
    "currency": "INR",
}


def project():
    return load_project(EXAMPLES / "faultline.project.yaml")


def scenario(name):
    return project().load_scenario(name)


def snapshot(records, role="final", quiescent=True):
    return {
        "snapshot_schema_version": 1,
        "snapshot_id": role,
        "role": role,
        "authority": "postgresql",
        "adapter_profile": "transfers_v1",
        "fixture": "two_accounts",
        "isolation": "repeatable_read_read_only",
        "started_ns": 1,
        "completed_ns": 2,
        "transaction": "hand-audited",
        "application_sessions": 0 if quiescent else 1,
        "complete": True,
        "quiescent": quiescent,
        "records": records,
        "row_counts": {k: len(v) for k, v in records.items()},
        "content_hash": digest(records),
    }


def baseline():
    return snapshot(transfer_verifier.fixture_records("two_accounts"), "baseline")


def committed(debit=True, credit=True, extra_debit=False):
    """Hand-audited final state for INTENT, written without the application."""
    records = transfer_verifier.fixture_records("two_accounts")
    records["transfers"] = [{**INTENT, "status": "committed"}]
    legs = []
    if debit:
        legs.append(("d1", "acct-a", "debit"))
    if extra_debit:
        legs.append(("d2", "acct-a", "debit"))
    if credit:
        legs.append(("c1", "acct-b", "credit"))
    records["ledger"] = [
        {
            "entry_id": entry,
            "account_id": account,
            "transfer_id": "transfer-1",
            "direction": direction,
            "amount_minor": 250000,
            "currency": "INR",
        }
        for entry, account, direction in legs
    ]
    for account in records["accounts"]:
        for _entry, owner, direction in legs:
            if owner == account["account_id"]:
                account["balance_minor"] += 250000 if direction == "credit" else -250000
    records["idempotency"] = [
        {
            "record_id": "k1",
            "account_id": "acct-a",
            "key": "transfer-1-key",
            "fingerprint": digest(INTENT),
            "transfer_id": "transfer-1",
        }
    ]
    return records


def attempts(status=201, body=None):
    body = {**INTENT, "status": "committed"} if body is None else body
    return [
        {
            "attempt_id": "move/1",
            "outcome": "http_response",
            "upstream": {"outcome": "http_response", "status": status, "body": body},
        }
    ]


def failing(results):
    return {(r["invariant_id"], f["code"]) for r in results for f in r["failures"]}


# --- verifier: hand-audited states -------------------------------------------------


def test_committed_transfer_passes_all_four_invariants():
    results = transfer_verifier.verify(
        scenario("control.yaml"), baseline(), snapshot(committed()), attempts()
    )
    assert {r["invariant_id"]: r["status"] for r in results} == dict.fromkeys(
        transfer_verifier.INVARIANTS, "PASS"
    )


def test_debit_without_credit_is_one_sided_and_breaks_conservation():
    results = transfer_verifier.verify(
        scenario("control.yaml"), baseline(), snapshot(committed(credit=False)), attempts()
    )
    assert ("transfers.atomicity", "one_sided_transfer") in failing(results)
    assert ("transfers.conservation", "balance_mismatch") in failing(results)


def test_duplicate_debit_is_caught_even_if_balances_reconcile():
    results = transfer_verifier.verify(
        scenario("control.yaml"), baseline(), snapshot(committed(extra_debit=True)), attempts()
    )
    assert ("transfers.atomicity", "one_sided_transfer") in failing(results)


def test_misleading_success_response_is_inconsistent():
    wrong = {**INTENT, "amount_minor": 1, "status": "committed"}
    results = transfer_verifier.verify(
        scenario("control.yaml"), baseline(), snapshot(committed()), attempts(body=wrong)
    )
    assert ("transfers.idempotency_contract", "inconsistent_transfer_result") in failing(results)


def test_unrequested_rows_and_rejections_with_effects():
    records = committed()
    records["ledger"].append({**records["ledger"][0], "entry_id": "x", "transfer_id": "ghost"})
    records["accounts"][0]["balance_minor"] -= 250000
    results = transfer_verifier.verify(
        scenario("control.yaml"), baseline(), snapshot(records), attempts()
    )
    assert ("transfers.atomicity", "unrequested_effect") in failing(results)
    raw = load_document(EXAMPLES / "control.yaml")
    raw["operations"][0]["expectation"] = "insufficient_funds"
    rejected = project().project.scenario(raw)
    results = transfer_verifier.verify(
        rejected,
        baseline(),
        snapshot(committed()),
        attempts(409, {"code": "insufficient_funds", "message": "x"}),
    )
    assert ("transfers.atomicity", "rejected_transfer_has_effects") in failing(results)


def test_unquiescent_final_state_is_unknown_not_pass():
    results = transfer_verifier.verify(
        scenario("control.yaml"), baseline(), snapshot(committed(), quiescent=False), attempts()
    )
    assert {r["status"] for r in results} == {"UNKNOWN"}


def test_wrong_opening_fixture_is_evidence_error():
    records = transfer_verifier.fixture_records("two_accounts")
    records["accounts"][0]["balance_minor"] = 1
    with pytest.raises(EvidenceError):
        transfer_verifier.validate_baseline(snapshot(records, "baseline"))


# --- checkpoint capability ---------------------------------------------------------


def test_checkpoint_trigger_contract():
    raw = load_document(EXAMPLES / "checkpoint-restart.yaml")
    fault = raw["faults"][0]
    no_name = deepcopy(raw)
    del no_name["faults"][0]["trigger"]["checkpoint"]
    with pytest.raises(ValidationError, match="checkpoint"):
        Scenario.model_validate(no_name)
    wrong_kind = deepcopy(raw)
    wrong_kind["faults"][0] = {
        **fault,
        "kind": "proxy_disconnect",
        "target": "database",
        "duration_ms": 100,
    }
    del wrong_kind["faults"][0]["downtime_ms"]
    with pytest.raises(ValidationError, match="process_restart"):
        Scenario.model_validate(wrong_kind)


def test_checkpoint_is_gated_by_variant_and_recorded_in_the_plan():
    loaded = project()
    plan_scenario = scenario("checkpoint-restart.yaml")
    with pytest.raises(ValueError, match="unsupported by variant correct"):
        resolve_adapter(loaded.project, plan_scenario, "correct")
    _adapter, _identity = resolve_adapter(loaded.project, plan_scenario, "non_atomic")
    assert plan_scenario.plan()["adapter_contract"]["checkpoints"] == ["transfer.debit_committed"]
    assert plan_scenario.checkpoints == ["transfer.debit_committed"]
    # The order adapter declares no checkpoints at all.
    raw = load_document(ROOT / "examples/orders/process-restart.yaml")
    raw["faults"][0]["trigger"] = {
        "event": "adapter_checkpoint",
        "checkpoint": "transfer.debit_committed",
        "operation_id": "purchase_a",
        "attempt": 1,
    }
    raw["operations"][0]["after_faults"] = []
    with pytest.raises(ValueError, match="does not declare"):
        resolve_adapter(None, Scenario.model_validate(raw), "correct")


def test_controller_crashes_only_after_the_bound_attempt_reaches_the_checkpoint():
    plan_scenario = scenario("checkpoint-restart.yaml")
    app = Mock(id="owned-app", attrs={"State": {"Running": False}, "Image": "sha256:x"})
    env = Mock(assert_owned=Mock(), containers={"application": app})
    replies = iter(
        [
            {"outcome": "http_response", "status": 200, "body": {"checkpoints": []}},
            {
                "outcome": "http_response",
                "status": 200,
                "body": {
                    "checkpoints": [
                        {"checkpoint": "transfer.debit_committed", "attempt_id": "other/1"},
                        {"checkpoint": "transfer.debit_committed", "attempt_id": "move/1"},
                    ]
                },
            },
        ]
    )
    env.call.side_effect = lambda *a, **k: next(replies)
    now, events, applied = [0.0], [], set()
    controller = FaultController(
        env,
        plan_scenario,
        lambda kind, payload, **refs: events.append((kind, refs)),
        applied,
        clock=lambda: now[0],
    )
    controller.start()
    controller.tick()
    env.call.assert_not_called()  # nothing is watched before the attempt is admitted
    controller.on_event("attempt_dispatched", {}, operation_id="move", attempt_id="move/1")
    assert controller.pending()
    now[0] = 1.0
    controller.tick()
    app.kill.assert_not_called()
    now[0] = 2.0
    controller.tick()
    app.kill.assert_called_once_with(signal="SIGKILL")
    kinds = [kind for kind, _refs in events]
    assert kinds.index("adapter_checkpoint") < kinds.index("fault_applied")
    assert applied == {"crash_after_debit"}
    request = json.loads(env.call.call_args.args[2])
    assert request["path"] == "/_faultline/checkpoints" and request["route"] == "transfers_api"


def test_missed_checkpoint_stops_watching_and_leaves_fault_unapplied():
    controller = FaultController(Mock(), scenario("checkpoint-restart.yaml"), Mock(), set())
    controller.start()
    controller.on_event("attempt_dispatched", {}, operation_id="move", attempt_id="move/1")
    controller.on_event("client_outcome", {}, operation_id="move", attempt_id="move/1")
    assert not controller.pending() and controller.applied == set()


def test_transfer_project_uses_its_own_adapter_and_readiness():
    loaded = project()
    adapter, identity = resolve_adapter(loaded.project, scenario("rejections.yaml"), "correct")
    description = adapter.description
    assert description.adapter_id == "bundled_transfers" and description.profile == "transfers_v1"
    assert "http_health" in description.readiness_checks
    assert identity["package"] == "faultline"
