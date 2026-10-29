import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from faultline.adapter_loader import resolve_adapter
from faultline.artifacts import Bundle
from faultline.cli import app
from faultline.models import Scenario, digest, load_scenario
from faultline.replay import (
    ReplayRejected,
    check_platform,
    comparison_differences,
    comparison_scenario,
    load_source,
    replay_scenario,
)
from faultline.verifier import EvidenceError

ROOT = Path(__file__).resolve().parents[1]
SIGNATURE = "c" * 64
FAILURE = {"code": "duplicate_debit", "expected": 1, "observed": 2, "evidence": ["x"]}
PLATFORM = {
    "os": "Linux",
    "architecture": "x86_64",
    "kernel": "6.6.87",
    "docker": "28.3.2",
    "images": {"runtime": "sha256:runtime", "postgres": "sha256:pg", "proxy": "sha256:proxy"},
}


def source_bundle(output, example="lost-response.yaml", variant="unsafe", verdict="FAIL"):
    scenario = load_scenario(ROOT / "examples/orders" / example)
    adapter, identity = resolve_adapter(None, scenario, variant)
    bundle = Bundle(output, scenario, variant)
    bundle.manifest["platform"] = PLATFORM
    bundle.manifest["adapter"] = {
        "adapter_id": adapter.description.adapter_id,
        "boundary_version": 1,
        "package": identity["package"],
        "package_version": identity["package_version"],
        "content_hash": identity["content_hash"],
        "artifact": "adapter.json",
    }
    bundle.save_manifest()
    bundle.event("example", {"value": 1})
    failing = verdict == "FAIL"
    bundle.finalize(
        {
            "verdict": verdict,
            "lifecycle": "completed",
            "invariants": [
                {
                    "invariant_id": "orders.at_most_one_debit",
                    "version": 1,
                    "status": "FAIL" if failing else "PASS",
                    "logical_id": "purchase-a",
                    "failures": [FAILURE] if failing else [],
                    "signatures": [SIGNATURE] if failing else [],
                }
            ],
            "diagnostics": [],
            "coverage": {"required_applied": 1, "required_total": 1},
            "cleanup": {"complete": True},
        }
    )
    return bundle, scenario


def test_strict_replay_rebuilds_the_exact_plan(tmp_path):
    bundle, scenario = source_bundle(tmp_path)
    source = load_source(bundle.path)
    replayed = replay_scenario(source)
    assert digest(replayed.plan()) == bundle.manifest["plan_hash"]
    check_platform(source, dict(PLATFORM))


def test_generated_values_and_generator_provenance_survive_replay(tmp_path):
    bundle, scenario = source_bundle(tmp_path, "generated.yaml", "correct", "PASS")
    replayed = replay_scenario(load_source(bundle.path))
    assert replayed.generators == []
    assert replayed.plan()["generators"] == scenario.plan()["generators"] != []
    assert digest(replayed.plan()) == digest(scenario.plan())


def test_modified_adapter_or_engine_is_rejected(tmp_path):
    bundle, _ = source_bundle(tmp_path)
    manifest_path = bundle.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["adapter"]["content_hash"] = "0" * 64
    manifest["engine_version"] = "0.0.0"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ReplayRejected) as caught:
        replay_scenario(load_source(bundle.path))
    fields = {d["field"] for d in caught.value.differences}
    assert fields == {"adapter.content_hash", "engine_version"}


def test_missing_or_changed_image_is_rejected(tmp_path):
    bundle, _ = source_bundle(tmp_path)
    source = load_source(bundle.path)
    changed = json.loads(json.dumps(PLATFORM))
    changed["images"]["runtime"] = "sha256:rebuilt"
    del changed["images"]["proxy"]
    changed["kernel"] = "6.12"
    with pytest.raises(ReplayRejected) as caught:
        check_platform(source, changed)
    assert {d["field"] for d in caught.value.differences} == {
        "platform.images.runtime",
        "platform.images.proxy",
        "platform.kernel",
    }


def test_unsupported_plan_versions_are_refused():
    scenario = load_scenario(ROOT / "examples/orders/control.yaml")
    resolve_adapter(None, scenario, "correct")
    plan = scenario.plan()
    for key in ("plan_schema_version", "fault_semantics_version", "canonicalization_version"):
        with pytest.raises(ValueError, match="unsupported"):
            Scenario.from_plan({**plan, key: 2})
    unexpanded = json.loads(json.dumps(plan))
    unexpanded["scenario"]["generators"] = [{"id": "g", "kind": "x", "count": 1, "parameters": {}}]
    with pytest.raises(ValueError, match="unexpanded"):
        Scenario.from_plan(unexpanded)


def test_source_without_environment_identity_is_not_replayable(tmp_path):
    scenario = load_scenario(ROOT / "examples/orders/control.yaml")
    bundle = Bundle(tmp_path, scenario, "correct")
    bundle.event("example", {})
    bundle.finalize(
        {
            "verdict": "ERROR",
            "invariants": [],
            "diagnostics": [],
            "coverage": {"required_applied": 0, "required_total": 0},
            "cleanup": {"complete": True},
        }
    )
    with pytest.raises(EvidenceError, match="environment identity"):
        load_source(bundle.path)


def test_comparison_records_differences_but_rejects_semantic_drift(tmp_path):
    bundle, _ = source_bundle(tmp_path)
    source = load_source(bundle.path)
    scenario, identity = comparison_scenario(source, None, "correct")
    changed = {**PLATFORM, "images": {**PLATFORM["images"], "runtime": "sha256:fixed"}}
    found = comparison_differences(source, scenario, identity, changed, "correct")
    assert {d["field"] for d in found} == {"variant", "platform.images.runtime"}
    drifted = json.loads(json.dumps(source.plan))
    drifted["adapter_contract"]["fixture_hash"] = "1" * 64
    drifted["invariant_versions"]["orders.at_most_one_debit"] = 2
    with pytest.raises(ReplayRejected) as caught:
        comparison_scenario(
            source.__class__(**{**source.__dict__, "plan": drifted}), None, "correct"
        )
    assert {d["field"] for d in caught.value.differences} == {
        "plan.adapter_contract.fixture_hash",
        "plan.invariant_versions.orders.at_most_one_debit",
    }


def fake_runs(verdicts, lineages):
    sequence = iter(verdicts)

    def run_one(scenario, variant, output, **kwargs):
        lineages.append(kwargs["lineage"])
        verdict, signatures = next(sequence)
        report = {
            "run_id": f"run-{len(lineages)}",
            "verdict": verdict,
            "lifecycle": "completed",
            "invariants": [
                {
                    "invariant_id": "orders.at_most_one_debit",
                    "version": 1,
                    "logical_id": "purchase-a",
                    "status": "FAIL" if signatures else "PASS",
                    "failures": [{"code": "duplicate_debit"} for _ in signatures],
                    "signatures": signatures,
                }
            ],
            "diagnostics": [],
            "coverage": {},
            "cleanup": {"complete": True},
        }
        return output / "runs" / report["run_id"], report

    return run_one


def test_replay_cli_counts_reproductions_and_links_lineage(monkeypatch, tmp_path):
    bundle, _ = source_bundle(tmp_path)
    client = Mock()
    monkeypatch.setattr("faultline.cli.client_and_preflight", lambda **kw: (client, PLATFORM))
    lineages = []
    outcomes = [("FAIL", [SIGNATURE]), ("PASS", []), ("FAIL", [SIGNATURE])]
    monkeypatch.setattr("faultline.cli.run_one", fake_runs(outcomes, lineages))
    result = CliRunner().invoke(
        app,
        ["--output", str(tmp_path), "--format", "json", "replay", bundle.run_id, "--repeat", "3"],
    )
    assert result.exit_code == 1, result.stdout
    envelope = json.loads(result.stdout)
    summary = envelope["summary"]
    assert summary["matching_failures"] == 2
    assert summary["reproduction"]["per_started"]["ratio"] == "2/3"
    assert all(item["origin"] == "replay" for item in lineages)
    assert {item["source_run_id"] for item in lineages} == {bundle.run_id}
    assert {item["group_id"] for item in lineages} == {envelope["group_id"]}
    client.close.assert_called_once()
    report = CliRunner().invoke(
        app, ["--output", str(tmp_path), "--format", "json", "report", envelope["group_id"]]
    )
    assert report.exit_code == 1
    group = json.loads(report.stdout)["summary"]
    assert group["kind"] == "replay" and group["status"] == "completed"
    assert group["counts"] == {"PASS": 1, "FAIL": 2, "INCONCLUSIVE": 0, "ERROR": 0}


def test_replay_cli_rejects_environment_drift_before_any_run(monkeypatch, tmp_path):
    bundle, _ = source_bundle(tmp_path)
    client = Mock()
    drifted = {**PLATFORM, "docker": "29.0.0"}
    monkeypatch.setattr("faultline.cli.client_and_preflight", lambda **kw: (client, drifted))
    run_one = Mock(side_effect=AssertionError("must not provision"))
    monkeypatch.setattr("faultline.cli.run_one", run_one)
    result = CliRunner().invoke(
        app, ["--output", str(tmp_path), "--format", "json", "replay", bundle.run_id]
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["diagnostics"][0]["code"] == "replay_incompatible"
    assert envelope["summary"]["differences"][0]["field"] == "platform.docker"
    client.close.assert_called_once()
    assert not (tmp_path / "groups").exists()


def test_replay_requires_a_signature_present_in_the_source(monkeypatch, tmp_path):
    bundle, _ = source_bundle(tmp_path)
    monkeypatch.setattr("faultline.cli.client_and_preflight", Mock(side_effect=AssertionError))
    result = CliRunner().invoke(
        app,
        ["--output", str(tmp_path), "replay", bundle.run_id, "--signature", "d" * 64],
    )
    assert result.exit_code == 2
    assert "choices" in result.stdout


def test_compare_cli_records_variant_and_image_differences(monkeypatch, tmp_path):
    bundle, _ = source_bundle(tmp_path)
    fixed = {**PLATFORM, "images": {**PLATFORM["images"], "runtime": "sha256:fixed"}}
    monkeypatch.setattr("faultline.cli.client_and_preflight", lambda **kw: (Mock(), fixed))
    lineages = []
    monkeypatch.setattr("faultline.cli.run_one", fake_runs([("PASS", [])] * 2, lineages))
    result = CliRunner().invoke(
        app,
        [
            "--output",
            str(tmp_path),
            "--format",
            "json",
            "compare",
            bundle.run_id,
            "--variant",
            "correct",
            "--repeat",
            "2",
        ],
    )
    assert result.exit_code == 0, result.stdout
    summary = json.loads(result.stdout)["summary"]
    assert {d["field"] for d in summary["differences"]} == {"variant", "platform.images.runtime"}
    assert summary["matching_failures"] == 0
    assert lineages[0]["origin"] == "comparison"
    assert lineages[0]["comparison"]["differences"] == summary["differences"]
