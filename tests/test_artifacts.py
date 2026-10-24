import json
from pathlib import Path

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


def test_signal_after_durable_append_does_not_duplicate_sequence(monkeypatch, tmp_path, scenario):
    import os

    bundle = Bundle(tmp_path, scenario, "correct")
    fsync = os.fsync

    def interrupt(descriptor):
        fsync(descriptor)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr("faultline.artifacts.os.fsync", interrupt)
        with pytest.raises(KeyboardInterrupt):
            bundle.event("ready", {})
    assert bundle.sequence == 1
    bundle.event("cleanup", {})
    bundle.finalize(
        {
            "verdict": "INCONCLUSIVE",
            "invariants": [],
            "diagnostics": [],
            "coverage": {"required_applied": 0, "required_total": 1},
            "cleanup": {"complete": True},
        }
    )
    assert read_bundle(bundle.path)[1]["last_sequence"] == 2


def test_partial_append_is_preserved_and_never_finalized(monkeypatch, tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    original_open = Path.open

    class BrokenWriter:
        def __enter__(self):
            self.stream = original_open(bundle.path / "history.ndjson", "ab")
            return self

        def write(self, raw):
            self.stream.write(raw[:10])
            raise OSError("partial disk write")

        def __exit__(self, *args):
            self.stream.close()

    def opened(path, mode="r", *args, **kwargs):
        if path.name == "history.ndjson" and mode == "ab":
            return BrokenWriter()
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", opened)
        with pytest.raises(OSError, match="partial disk write"):
            bundle.event("ready", {})
    partial = (bundle.path / "history.ndjson").read_bytes()
    assert len(partial) == 10
    with pytest.raises(OSError, match="incomplete history"):
        bundle.event("cleanup", {})
    with pytest.raises(OSError, match="Incomplete history"):
        bundle.finalize({})
    assert (bundle.path / "history.ndjson").read_bytes() == partial
    assert not (bundle.path / "report.json").exists()
