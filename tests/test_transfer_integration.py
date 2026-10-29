"""Opt-in Docker acceptance for the independent transfer domain (AT-023)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from faultline.artifacts import read_bundle

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FAULTLINE_INTEGRATION") != "1",
        reason="set FAULTLINE_INTEGRATION=1 after scripts/build-images.sh",
    ),
]


def run(tmp_path, scenario, variant):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "faultline",
            "--project",
            str(ROOT / "examples/transfers/faultline.project.yaml"),
            "--output",
            str(tmp_path),
            "--format",
            "json",
            "run",
            scenario,
            "--variant",
            variant,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return json.loads(result.stdout)


def test_checkpoint_crash_exposes_one_sided_transfer(tmp_path):
    envelope = run(tmp_path, "checkpoint-restart.yaml", "non_atomic")
    assert envelope["exit_code"] == 1, envelope
    path = Path(envelope["artifact_paths"][0])
    manifest, report = read_bundle(path)
    assert manifest["adapter"]["adapter_id"] == "bundled_transfers"
    failures = {(f["invariant_id"], x["code"]) for f in report["invariants"] for x in f["failures"]}
    assert ("transfers.atomicity", "one_sided_transfer") in failures
    events = [json.loads(line) for line in (path / "history.ndjson").read_text().splitlines()]
    kinds = [e["type"] for e in events]
    # The crash is authorized by the acknowledged checkpoint of the bound attempt.
    assert kinds.index("adapter_checkpoint") < kinds.index("fault_applied")
    checkpoint = next(e for e in events if e["type"] == "adapter_checkpoint")
    assert checkpoint["attempt_id"] == "move/1"
    final = json.loads((path / "snapshots/final.json").read_text())["records"]
    directions = sorted(r["direction"] for r in final["ledger"])
    assert directions == ["debit"]  # the raw rows, not the checkpoint, show the defect
    assert report["cleanup"]["complete"]


def test_atomic_variant_refuses_the_checkpoint_and_survives_a_restart(tmp_path):
    refused = run(tmp_path, "checkpoint-restart.yaml", "correct")
    assert refused["exit_code"] == 2 and not refused["run_ids"]
    assert "unsupported by variant correct" in refused["diagnostics"][0]["message"]
    restarted = run(tmp_path, "process-restart.yaml", "correct")
    assert restarted["exit_code"] == 0, restarted
    _manifest, report = read_bundle(Path(restarted["artifact_paths"][0]))
    assert report["coverage"]["required_applied"] == 1
