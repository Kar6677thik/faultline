import json

import pytest

from faultline.artifacts import Bundle, read_bundle
from faultline.verifier import EvidenceError


def completed_bundle(tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    bundle.event("example", {"value": 1})
    bundle.finalize(
        {
            "verdict": "INCONCLUSIVE",
            "invariants": [],
            "diagnostics": [],
            "coverage": {"required_applied": 0, "required_total": 1},
            "cleanup": {"complete": True},
        }
    )
    return bundle


def test_roundtrip_and_tampered_history(tmp_path, scenario):
    bundle = completed_bundle(tmp_path, scenario)
    manifest, report = read_bundle(bundle.path)
    assert report["run_id"] == manifest["run_id"] == bundle.run_id
    with (bundle.path / "history.ndjson").open("a") as stream:
        stream.write("{truncated")
    with pytest.raises(EvidenceError, match="corrupt"):
        read_bundle(bundle.path)


def test_incomplete_bundle_cannot_be_read_as_pass(tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    with pytest.raises(EvidenceError, match="incomplete"):
        read_bundle(bundle.path)


def test_manifest_cannot_escape_bundle(tmp_path, scenario):
    bundle = completed_bundle(tmp_path, scenario)
    outside = tmp_path / "outside.txt"
    outside.write_text("not evidence")
    path = bundle.path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"][str(outside.resolve())] = {"sha256": "not relevant", "bytes": 0}
    path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="unsafe artifact path"):
        read_bundle(bundle.path)


def test_budget_is_enforced_before_append(tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    bundle.budget = 100
    with pytest.raises(OSError, match="budget"):
        bundle.event("huge", {"data": "x" * 101})
    assert bundle.sequence == 0
