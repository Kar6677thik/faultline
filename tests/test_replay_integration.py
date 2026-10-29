"""Opt-in Docker acceptance for strict replay and explicit comparison (AT-014/AT-015)."""

import json
import os
import subprocess
import sys
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


def cli(project_root, *args, timeout=600):
    result = subprocess.run(
        [sys.executable, "-m", "faultline", "--format", "json", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, json.loads(result.stdout), result.stderr


def test_strict_replay_reproduces_and_comparison_records_the_fix(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for source_file in (ROOT / "examples/orders").glob("*.yaml"):
        (project / source_file.name).write_bytes(source_file.read_bytes())

    code, source, stderr = cli(project, "run", "lost-response.yaml", "--variant", "unsafe")
    assert code == 1, (source, stderr)
    source_id = source["run_ids"][0]
    source_manifest, source_report = read_bundle(Path(source["artifact_paths"][0]))

    catalog = failure_signatures(source_report)
    # An unsafe duplicate also breaks atomicity and completion; target the debit itself.
    target = next(key for key, item in catalog.items() if item["code"] == "duplicate_debit")
    assert len(catalog) > 1
    code, replay, stderr = cli(project, "replay", source_id, "--repeat", "3", "--signature", target)
    assert code == 1, (replay, stderr)
    summary = replay["summary"]
    print(f"replay group {replay['group_id']}: {json.dumps(summary)}", flush=True)
    assert summary["started"] == 3 and summary["counts"]["ERROR"] == 0
    assert summary["matching_failures"] == 3
    assert summary["reproduction"]["per_started"]["ratio"] == "3/3"
    for path in replay["artifact_paths"]:
        manifest, report = read_bundle(Path(path))
        assert manifest["origin"] == "replay"
        assert manifest["source_run_id"] == source_id
        assert manifest["group_id"] == replay["group_id"]
        # Exact plan, fixture, and image identity survive replay.
        assert manifest["plan_hash"] == source_manifest["plan_hash"]
        assert manifest["platform"] == source_manifest["platform"]
        assert (Path(path) / "plan.json").read_bytes() == (
            Path(source["artifact_paths"][0]) / "plan.json"
        ).read_bytes()
        # Every trial starts from the same fresh baseline fixture.
        assert (Path(path) / "snapshots/baseline.json").exists()
        baseline = json.loads((Path(path) / "snapshots/baseline.json").read_text())
        assert baseline["content_hash"] == source_report_baseline(source)
        assert report["cleanup"]["complete"]

    code, compare, stderr = cli(
        project,
        "compare",
        source_id,
        "--variant",
        "correct",
        "--repeat",
        "2",
        "--signature",
        target,
    )
    assert code == 0, (compare, stderr)
    fields = {d["field"] for d in compare["summary"]["differences"]}
    print(f"comparison differences: {sorted(fields)}", flush=True)
    assert "variant" in fields and "platform.variant" in fields
    assert compare["summary"]["counts"]["PASS"] == 2
    assert compare["summary"]["matching_failures"] == 0
    for path in compare["artifact_paths"]:
        manifest, _ = read_bundle(Path(path))
        assert manifest["origin"] == "comparison"
        assert manifest["plan_hash"] == source_manifest["plan_hash"]

    code, group, _ = cli(project, "report", replay["group_id"])
    assert code == 1 and group["summary"]["kind"] == "replay"
    client, _ = client_and_preflight(require_images=False)
    try:
        filters = {"label": "io.faultline.managed=true"}
        assert not client.containers.list(all=True, filters=filters)
    finally:
        client.close()


def source_report_baseline(envelope):
    path = Path(envelope["artifact_paths"][0]) / "snapshots/baseline.json"
    return json.loads(path.read_text())["content_hash"]
