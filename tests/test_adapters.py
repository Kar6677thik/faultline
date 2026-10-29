"""The judge's contract must be checked as carefully as the application's output."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from faultline.adapters import (
    BUNDLED,
    AdapterError,
    CheckedAdapter,
    OrdersAdapter,
    bundled_adapter,
    source_identity,
)
from faultline.artifacts import read_bundle
from faultline.deadlines import DeadlineExceeded
from faultline.engine import run_one
from faultline.models import digest
from faultline.verifier import EvidenceError, verify


def test_capability_mismatch_refuses_before_docker_or_output(monkeypatch, scenario, tmp_path):
    preflight = Mock(side_effect=AssertionError("must not reach Docker"))
    monkeypatch.setattr("faultline.engine.client_and_preflight", preflight)
    with pytest.raises(ValueError, match="unsupported adapter capabilities"):
        run_one(scenario, "unregistered", tmp_path / "output")
    preflight.assert_not_called()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"boundary_version": True},
        {"boundary_version": 2},
        {"external_registration": "true"},
        {"unexpected": True},
        {"concurrency": "1"},
    ],
)
def test_malformed_description_is_a_contract_error(change):
    implementation = OrdersAdapter()
    implementation.describe = Mock(return_value={**implementation.describe(), **change})
    with pytest.raises(AdapterError) as error:
        CheckedAdapter(implementation)
    assert error.value.hook == "describe"
    assert error.value.code == "adapter_contract_error"


@pytest.mark.parametrize("field", ["faults", "operations", "invariants"])
def test_missing_capability_is_rejected(scenario, field):
    implementation = OrdersAdapter()
    description = implementation.describe()
    description[field] = {} if field == "invariants" else []
    implementation.describe = Mock(return_value=description)
    with pytest.raises(ValueError, match="unsupported adapter capabilities"):
        CheckedAdapter(implementation).validate(scenario, "correct")


@pytest.mark.parametrize("acknowledgment", [None, {}, {"prepared": False}, {"prepared": 1}])
def test_prepare_requires_acknowledgment_and_never_observes_partial_setup(acknowledgment):
    environment = Mock()
    environment.call.return_value = acknowledgment
    with pytest.raises(AdapterError) as error:
        bundled_adapter().prepare(environment, "correct")
    assert error.value.hook == "prepare"
    environment.snapshot.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot_schema_version": True},
        {"role": "baseline"},
        {"snapshot_id": "baseline"},
        {"content_hash": "0" * 64},
        {"quiescent": "true"},
        {"row_counts": {}},
        {"adapter_profile": "transfers_v1"},
        {"isolation": "read_committed"},
        {"application_sessions": 1},
        {"transaction": None},
    ],
)
def test_boundary_checks_snapshot_even_if_hook_forgets_to(correct_snapshot, change):
    implementation = OrdersAdapter()
    implementation.snapshot = Mock(return_value={**correct_snapshot, **change})
    with pytest.raises(AdapterError) as error:
        CheckedAdapter(implementation).snapshot(Mock(), "final")
    assert error.value.code == "adapter_contract_error"


def test_incomplete_baseline_is_error_but_incomplete_final_is_evidence(baseline, correct_snapshot):
    implementation = OrdersAdapter()
    implementation.prepare = Mock(return_value={**baseline, "complete": False})
    adapter = CheckedAdapter(implementation)
    with pytest.raises(AdapterError):
        adapter.prepare(Mock(), "correct")
    implementation.snapshot = Mock(return_value={**correct_snapshot, "complete": False})
    assert adapter.snapshot(Mock(), "final")["complete"] is False


@pytest.mark.parametrize("value", [None, "true", 1, {"ready": True}])
def test_truthy_quiescence_is_not_a_graceful_exit(value):
    environment = Mock()
    environment.drain.return_value = value
    with pytest.raises(AdapterError):
        bundled_adapter().quiesce(environment, 1000)


@pytest.fixture
def valid_findings(scenario, baseline, correct_snapshot):
    return verify(scenario, baseline, correct_snapshot, [])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.clear(),
        lambda rows: rows.pop(),
        lambda rows: rows.append(deepcopy(rows[0])),
        lambda rows: rows.__setitem__(1, deepcopy(rows[0])),
        lambda rows: rows[0].update(invariant_id="invented.rule"),
        lambda rows: rows[0].update(version=2),
        lambda rows: rows[0].update(version=True),
        lambda rows: rows[0].update(logical_id="another-purchase"),
        lambda rows: rows[0].update(status="ERROR"),
        lambda rows: rows[0].update(status="FAIL"),
        lambda rows: rows[0].update(status="UNKNOWN"),
        lambda rows: rows[0].update(evidence=[]),
        lambda rows: rows[0].update(signatures=["0" * 64]),
        lambda rows: rows[0].update(extra="ignored?"),
        lambda rows: rows[0].update(reason="x" * 1048576),
    ],
)
def test_invalid_verifier_output_is_never_a_business_verdict(
    scenario, baseline, correct_snapshot, valid_findings, mutate
):
    mutate(valid_findings)
    implementation = OrdersAdapter()
    implementation.verify = Mock(return_value=valid_findings)
    with pytest.raises(AdapterError) as error:
        CheckedAdapter(implementation).verify(
            scenario, baseline, correct_snapshot, [], workload_complete=True
        )
    assert error.value.code == "adapter_contract_error"
    assert error.value.hook == "verify"


def test_incomplete_work_retains_unknown(scenario, baseline, correct_snapshot):
    findings = bundled_adapter().verify(
        scenario, baseline, correct_snapshot, [], workload_complete=False
    )
    completion = next(row for row in findings if row["invariant_id"] == "orders.completed_purchase")
    assert completion["status"] == "UNKNOWN"
    assert completion["reason"] == "workload_incomplete"


def test_hook_exception_diagnostic_does_not_expose_exception_payload():
    environment = Mock()
    environment.drain.side_effect = RuntimeError("password=secret-test-token")
    with pytest.raises(AdapterError) as error:
        bundled_adapter().quiesce(environment, 1000)
    diagnostic = error.value.diagnostic()
    assert diagnostic["code"] == "adapter_hook_error"
    assert diagnostic["hook"] == "quiesce"
    assert "secret-test-token" not in json.dumps(diagnostic)


@pytest.mark.parametrize("exception", [DeadlineExceeded("draining", "stage"), KeyboardInterrupt()])
def test_control_flow_interruptions_are_not_wrapped_as_hook_errors(exception):
    environment = Mock()
    environment.drain.side_effect = exception
    with pytest.raises(type(exception)) as error:
        bundled_adapter().quiesce(environment, 1000)
    assert error.value is exception


def test_returned_snapshot_is_detached_from_hook_memory(correct_snapshot):
    environment = Mock()
    environment.snapshot.return_value = correct_snapshot
    observed = bundled_adapter().snapshot(environment, "final")
    correct_snapshot["records"]["ledger"].clear()
    assert len(observed["records"]["ledger"]) == 1


def test_source_inventory_is_stable_and_includes_the_judge():
    identity = source_identity()
    assert identity == source_identity()
    assert {"adapters.py", "verifier.py", "models.py"} <= identity["sources"].keys()
    assert identity["content_hash"] == digest(identity["sources"])
    changed = {**identity["sources"], "verifier.py": "0" * 64}
    assert digest(changed) != identity["content_hash"]


@pytest.mark.parametrize("broken_hook", ["prepare", "readiness", "snapshot", "verify"])
def test_hook_failure_is_reported_with_cleanup_and_identity(
    monkeypatch, tmp_path, scenario, baseline, correct_snapshot, broken_hook
):
    environment = Mock()
    environment.cleanup_remaining = []
    environment.snapshot.side_effect = lambda role: deepcopy(
        baseline if role == "baseline" else correct_snapshot
    )
    environment.drain.return_value = True
    environment.call.side_effect = lambda role, *args, **kwargs: (
        {"prepared": True}
        if role == "observer"
        else {"outcome": "http_response", "status": 201, "body": {}}
    )
    environment.cleanup.return_value = {"complete": True, "remaining_resources": []}
    monkeypatch.setattr("faultline.engine.Environment", lambda *args: environment)
    implementation = OrdersAdapter()
    if broken_hook == "snapshot":
        original = implementation.snapshot
        implementation.snapshot = lambda env, role: (
            original(env, role) if role == "baseline" else "broken-contract"
        )
    else:
        setattr(implementation, broken_hook, Mock(return_value="broken-contract"))
    monkeypatch.setitem(BUNDLED, "bundled_orders", lambda: CheckedAdapter(implementation))
    # No deliberate timeout needed in these harness failure checks.
    raw = scenario.model_dump(by_alias=True)
    raw["faults"] = []
    scenario = type(scenario).model_validate(raw)
    path, report = run_one(
        scenario, "correct", tmp_path, client=Mock(), platform_info={"images": {}}
    )
    assert report["verdict"] == "ERROR"
    assert any(d.get("hook") == broken_hook for d in report["diagnostics"])
    assert not report["invariants"]
    environment.cleanup.assert_called_once()
    assert report["cleanup"]["complete"]
    manifest, reread = read_bundle(path)
    assert reread["adapter"] == manifest["adapter"]
    assert (path / "adapter.json").exists()
    adapter_artifact = json.loads((path / "adapter.json").read_text())
    assert adapter_artifact["identity"]["content_hash"] == report["adapter"]["content_hash"]
    if broken_hook == "prepare":
        environment.start_application.assert_not_called()
    (path / "adapter.json").write_text("{}")
    with pytest.raises(EvidenceError, match="corrupt artifact"):
        read_bundle(path)
