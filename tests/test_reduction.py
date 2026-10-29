import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from faultline.adapter_loader import resolve_adapter
from faultline.artifacts import Bundle
from faultline.cli import app
from faultline.models import Scenario, load_document, load_scenario
from faultline.reduction import Reducer, objective, remove, trim_retry
from faultline.replay import load_source

ROOT = Path(__file__).resolve().parents[1]
TARGET = "e" * 64
OTHER = "f" * 64
FAILURE = {"code": "duplicate_debit", "expected": 1, "observed": 2, "evidence": ["x"]}


def finding(signatures, logical_id="purchase-a"):
    return {
        "invariant_id": "orders.at_most_one_debit",
        "version": 1,
        "status": "FAIL" if signatures else "PASS",
        "logical_id": logical_id,
        "failures": [FAILURE for _ in signatures],
        "signatures": list(signatures),
    }


def source_bundle(output, signatures=(TARGET,)):
    scenario = load_scenario(ROOT / "examples/orders/reduction-market.yaml")
    adapter, identity = resolve_adapter(None, scenario, "unsafe")
    bundle = Bundle(output, scenario, "unsafe")
    bundle.manifest["platform"] = {"os": "Linux", "images": {"runtime": "sha256:x"}}
    bundle.manifest["adapter"] = {
        "adapter_id": adapter.description.adapter_id,
        "package": identity["package"],
        "package_version": identity["package_version"],
        "content_hash": identity["content_hash"],
    }
    bundle.save_manifest()
    bundle.event("example", {})
    bundle.finalize(
        {
            "verdict": "FAIL",
            "lifecycle": "completed",
            "invariants": [finding(signatures)],
            "diagnostics": [],
            "coverage": {"required_applied": 1, "required_total": 1},
            "cleanup": {"complete": True},
        }
    )
    return load_source(bundle.path)


def duplicate_needs_retry_and_fault(scenario):
    """A fake application: the duplicate debit needs purchase_a, its fault, and a retry."""
    ops = {op.id: op for op in scenario.all_operations}
    faults = {fault.id for fault in scenario.faults}
    return (
        "purchase_a" in ops
        and "hide_first_response" in faults
        and ops["purchase_a"].retry.max_attempts >= 2
    )


class FakeLab:
    def __init__(self, tmp_path, decide=duplicate_needs_retry_and_fault, seconds=30):
        self.tmp_path, self.decide, self.seconds = tmp_path, decide, seconds
        self.now = 0.0
        self.calls = []

    def clock(self):
        return self.now

    def validate(self, source):
        def check(raw):
            scenario = Scenario.from_plan({**source.plan, "scenario": raw})
            resolve_adapter(None, scenario, "unsafe")
            return scenario

        return check

    def trial(self, scenario, purpose):
        self.now += self.seconds
        self.calls.append((purpose, len(scenario.all_operations)))
        outcome = (
            self.decide(scenario)
            if not callable(getattr(self, "override", None))
            else (self.override(scenario, purpose))
        )
        if outcome is True:
            verdict, signatures = "FAIL", [TARGET]
        elif outcome is False:
            verdict, signatures = "PASS", []
        else:
            verdict, signatures = outcome
        run_id = f"trial-{uuid4()}"
        path = self.tmp_path / "trials" / run_id
        path.mkdir(parents=True)
        (path / "report.json").write_text("{}")
        return path, {
            "run_id": run_id,
            "verdict": verdict,
            "lifecycle": "completed",
            "invariants": [finding(signatures)],
            "cleanup": {"complete": True},
        }


def reducer(tmp_path, source, lab, **kwargs):
    return Reducer(
        tmp_path,
        source,
        TARGET,
        validate=lab.validate(source),
        trial=lab.trial,
        clock=lab.clock,
        **kwargs,
    )


def digest_tree(path):
    return {
        str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(path.rglob("*"))
        if f.is_file()
    }


def test_twenty_operation_failure_reduces_and_validates(tmp_path):
    source = source_bundle(tmp_path)
    before = digest_tree(source.path)
    lab = FakeLab(tmp_path)
    record = reducer(tmp_path, source, lab, max_duration_ms=3600000).run()
    assert record["outcome"] == "reduction_verified"
    assert record["original_objective"] == [20, 1, 21]
    best = record["best"]
    assert best["objective"] == [1, 1, 2]
    assert len(best["removed_operations"]) == 19 and best["removed_faults"] == []
    # At least a 75% reduction in logical operations (AT-016's target).
    assert best["objective"][0] <= record["original_objective"][0] / 4
    assert record["baseline"]["matching"] == 5 and record["validation"]["matching"] == 5
    # Every started trial is recorded and consumed budget, including rejections.
    assert record["budget"]["runs_used"] == len(record["run_ids"]) == len(lab.calls)
    rejected = [c for c in record["candidates"] if c.get("valid") and not c["accepted"]]
    assert rejected and all(len(c["trials"]) == 1 for c in rejected)  # early rejection
    accepted = [c for c in record["candidates"] if c.get("accepted")]
    assert all(len(c["trials"]) == 3 for c in accepted)
    saved = json.loads((tmp_path / "groups" / f"{record['group_id']}.json").read_text())
    assert saved["outcome"] == "reduction_verified"
    reduced = json.loads((tmp_path / "groups" / best["scenario_file"]).read_text())
    assert [op["id"] for op in reduced["operations"]] == ["purchase_a"]
    assert digest_tree(source.path) == before  # original bundle untouched


def test_dependency_closure_removes_dependents_and_empty_barriers():
    raw = load_document(ROOT / "examples/orders/lost-response.yaml")
    base = raw["operations"][0]
    raw["operations"] = [
        base,
        {**base, "id": "after_a", "after": ["purchase_a"]},
        {**base, "id": "gated", "after_faults": ["hide_first_response"], "barrier": "pair"},
        {**base, "id": "member", "barrier": "pair"},
    ]
    raw["barriers"] = [{"id": "pair", "members": ["gated", "member"]}]
    # Removing the trigger operation removes its fault, then the fault-gated operation.
    result = remove(raw, operations=["purchase_a"])
    assert [op["id"] for op in result["operations"]] == ["member"]
    assert result["faults"] == []
    assert result["barriers"] == [{"id": "pair", "members": ["member"]}]
    # A barrier with no remaining members disappears.
    assert remove(raw, operations=["purchase_a", "member"])["barriers"] == []
    # Removing a fault removes operations gated on its application.
    without_fault = remove(raw, faults=["hide_first_response"])
    assert "gated" not in {op["id"] for op in without_fault["operations"]}
    assert raw["operations"][2]["id"] == "gated"  # input unchanged


def test_trimming_a_retry_drops_faults_on_the_removed_attempt():
    raw = load_document(ROOT / "examples/orders/lost-response.yaml")
    raw["faults"][0]["trigger"]["attempt"] = 2
    trimmed = trim_retry(raw, "purchase_a")
    assert trimmed["operations"][0]["retry"]["max_attempts"] == 1
    assert trimmed["operations"][0]["retry"]["backoff_ms"] == []
    assert trimmed["faults"] == []
    assert trim_retry(trimmed, "purchase_a") is None
    assert objective(trimmed) < objective(raw)


def test_unrelated_signature_never_satisfies_the_target(tmp_path):
    source = source_bundle(tmp_path)
    lab = FakeLab(tmp_path)
    full = 20

    def decide(scenario):
        if len(scenario.all_operations) == full and duplicate_needs_retry_and_fault(scenario):
            return True
        # Smaller plans still fail, but with a different defect.
        return ("FAIL", [OTHER])

    lab.decide = decide
    # An exhaustive search with no accepted deletion needs about 50 single-trial candidates.
    record = reducer(tmp_path, source, lab, max_runs=100, max_duration_ms=3600000).run()
    assert record["outcome"] == "no_smaller_reproduction"
    assert record["termination_reason"] == "search_exhausted"
    assert record["best"] is None and record["validation"] is None
    tried = [c for c in record["candidates"] if c.get("trials")]
    assert tried and all(not t["matches_target"] for c in tried for t in c["trials"])


def test_unstable_or_erroring_baseline_stops_before_deletion(tmp_path):
    source = source_bundle(tmp_path)
    lab = FakeLab(tmp_path)
    outcomes = iter([True, True, True, False, False])
    lab.decide = lambda scenario: next(outcomes)
    record = reducer(tmp_path, source, lab).run()
    assert record["outcome"] == "baseline_unstable"
    assert record["baseline"]["matching"] == 3 and record["candidates"] == []

    lab = FakeLab(tmp_path)
    outcomes = iter([True, True, True, True, ("ERROR", [TARGET])])
    lab.decide = lambda scenario: next(outcomes)
    record = reducer(tmp_path, source, lab).run()
    # A matching violation inside an ERROR is not a reproduction.
    assert record["baseline"]["matching"] == 4 and record["baseline"]["errors"] == 1
    assert record["outcome"] == "baseline_unstable"


def test_run_budget_exhaustion_is_not_a_negative_result(tmp_path):
    source = source_bundle(tmp_path)
    lab = FakeLab(tmp_path, decide=lambda scenario: len(scenario.all_operations) == 20)
    record = reducer(tmp_path, source, lab, max_runs=12).run()
    # Five validation slots stay reserved, so only two candidate trials could start.
    assert record["budget"]["runs_used"] == 7
    assert record["outcome"] == "insufficient_budget"
    assert record["termination_reason"] == "budget_exhausted"


def test_time_exhaustion_before_validation_is_unverified(tmp_path):
    source = source_bundle(tmp_path)
    lab = FakeLab(tmp_path)
    slow = []

    def override(scenario, purpose):
        slow.append(purpose)
        if slow.count("candidate-2") == 3:
            lab.now += 3000  # the accepted candidate's last trial is unexpectedly slow
        return duplicate_needs_retry_and_fault(scenario)

    lab.override = override
    record = reducer(tmp_path, source, lab, max_duration_ms=3300000).run()
    assert record["best"] is not None
    assert record["outcome"] == "reduction_unverified"
    assert record["validation"] == {"completed": False}


def test_failed_final_validation_is_unverified(tmp_path):
    source = source_bundle(tmp_path)
    lab = FakeLab(tmp_path)
    lab.override = lambda scenario, purpose: (
        purpose != "validation" and duplicate_needs_retry_and_fault(scenario)
    )
    record = reducer(tmp_path, source, lab, max_duration_ms=3600000).run()
    assert record["outcome"] == "reduction_unverified"
    assert record["validation"]["completed"] and record["validation"]["matching"] == 0


def test_interrupted_trial_stops_search_and_persists(tmp_path):
    source = source_bundle(tmp_path)
    lab = FakeLab(tmp_path)
    original = lab.trial

    def trial(scenario, purpose):
        path, report = original(scenario, purpose)
        if len(lab.calls) == 2:
            report["lifecycle"] = "interrupted"
        return path, report

    lab.trial = trial
    search = reducer(tmp_path, source, lab)
    with pytest.raises(KeyboardInterrupt):
        search.run()
    saved = json.loads(search.path.read_text())
    assert saved["status"] == "interrupted" and len(saved["run_ids"]) == 2


def test_cli_requires_an_explicit_signature_when_several_exist(monkeypatch, tmp_path):
    source = source_bundle(tmp_path, signatures=(TARGET, OTHER))
    monkeypatch.setattr("faultline.cli.client_and_preflight", Mock(side_effect=AssertionError))
    result = CliRunner().invoke(
        app, ["--output", str(tmp_path), "--format", "json", "reduce", source.run_id]
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["diagnostics"][0]["code"] == "signature_required"
    assert set(envelope["summary"]["choices"]) == {TARGET, OTHER}
