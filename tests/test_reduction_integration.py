"""Opt-in Docker acceptance for dependency-aware reduction (AT-016)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from faultline.artifacts import read_bundle
from faultline.environment import client_and_preflight
from faultline.groups import failure_signatures

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FAULTLINE_INTEGRATION") != "1",
        reason="set FAULTLINE_INTEGRATION=1 with local Docker images built",
    ),
]


def cli(cwd, *args, timeout=600):
    result = subprocess.run(
        [sys.executable, "-m", "faultline", "--format", "json", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, json.loads(result.stdout), result.stderr


def tree_hashes(path):
    import hashlib

    return {
        str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(path.rglob("*"))
        if f.is_file()
    }


def test_twenty_operation_duplicate_debit_reduces_to_a_verified_smaller_plan(tmp_path):
    scenario = ROOT / "examples/orders/reduction-market.yaml"
    code, source, stderr = cli(tmp_path, "run", str(scenario), "--variant", "unsafe")
    assert code == 1, (source, stderr)
    source_path = Path(source["artifact_paths"][0])
    _manifest, report = read_bundle(source_path)
    assert len(report["attempts"]) >= 20
    catalog = failure_signatures(report)
    target = next(
        key
        for key, item in catalog.items()
        if item["code"] == "duplicate_debit" and item["logical_id"] == "purchase-a"
    )
    # Unrelated noise purchases are real successes, not hidden failures.
    assert all(item["logical_id"] == "purchase-a" for item in catalog.values())
    before = tree_hashes(source_path)

    started = time.monotonic()
    code, reduced, stderr = cli(
        tmp_path,
        "reduce",
        source["run_ids"][0],
        "--signature",
        target,
        "--max-runs",
        "80",
        "--max-duration-ms",
        "3600000",
        timeout=3700,
    )
    elapsed = time.monotonic() - started
    summary = reduced["summary"]
    print(f"reduction {reduced.get('group_id')} in {elapsed:.1f}s: {json.dumps(summary)}")
    assert code == 0, (summary, stderr[-4000:])
    assert summary["outcome"] == "reduction_verified"
    assert summary["original_objective"][0] == 20
    assert summary["best"]["objective"][0] <= 5  # at least a 75% reduction
    assert "hide_first_response" not in summary["best"]["removed_faults"]
    assert summary["baseline"]["matching"] >= 4 and summary["baseline"]["errors"] == 0
    assert summary["validation"]["matching"] >= 4 and summary["validation"]["errors"] == 0
    assert tree_hashes(source_path) == before  # the original bundle is immutable

    group = json.loads((tmp_path / ".faultline/groups" / f"{reduced['group_id']}.json").read_text())
    assert len(group["run_ids"]) == group["budget"]["runs_used"]
    for run_id in group["run_ids"]:
        manifest, trial = read_bundle(tmp_path / ".faultline/runs" / run_id)
        assert manifest["origin"] == "reduction" and manifest["group_id"] == reduced["group_id"]
        assert trial["cleanup"]["complete"]
    client, _ = client_and_preflight(require_images=False)
    try:
        assert not client.containers.list(all=True, filters={"label": "io.faultline.managed"})
    finally:
        client.close()
