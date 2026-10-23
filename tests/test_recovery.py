import json
import os
import shutil
from unittest.mock import Mock
from uuid import uuid4

import pytest
from docker.errors import NotFound
from typer.testing import CliRunner

from faultline.artifacts import Bundle
from faultline.cli import app
from faultline.engine import run_one
from faultline.locking import ActiveRunError, execution_lock
from faultline.recovery import read_ownership, recover, recovery_manifests


@pytest.fixture
def recorded_bundle(tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    bundle.manifest["resources"] = [
        {"kind": "container", "id": "a" * 64, "role": "application"},
        {"kind": "network", "id": "b" * 64, "role": "client"},
    ]
    bundle.save_manifest()
    return bundle


@pytest.mark.parametrize(
    "change",
    [
        {"bundle_schema_version": True},
        {"bundle_schema_version": 2},
        {"implementation_profile": "unknown"},
        {"run_id": str(uuid4())},
        {"ownership_nonce": ""},
        {"ownership_nonce": None},
        {"execution_root": None},
        {"resources": "all"},
        {"resources": [{"kind": "volume", "id": "a" * 64, "role": "client"}]},
        {"resources": [{"kind": "network", "id": "production", "role": "client"}]},
        {"resources": [{"kind": "network", "id": "a" * 12, "role": "client"}]},
        {"resources": [{"kind": "network", "id": "a" * 64, "role": "database"}]},
        {"resources": [{"kind": [], "id": "a" * 64, "role": "client"}]},
        {"resources": [{"kind": "network", "id": "a" * 64}]},
        {"resources": [{"kind": "network", "id": "a" * 64, "role": "client"}] * 2},
        {"resources": [{}] * 7},
    ],
)
def test_invalid_inventory_refused_before_daemon(monkeypatch, tmp_path, recorded_bundle, change):
    recorded_bundle.manifest.update(change)
    recorded_bundle.save_manifest()
    preflight = Mock(side_effect=AssertionError("invalid input must not reach Docker"))
    monkeypatch.setattr("faultline.cli.client_and_preflight", preflight)
    result = CliRunner().invoke(
        app, ["--output", str(tmp_path), "--format", "json", "clean", "--stale"]
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["exit_code"] == 2
    preflight.assert_not_called()


def test_duplicate_fields_and_oversized_manifest_are_rejected(tmp_path, recorded_bundle):
    path = recorded_bundle.path / "manifest.json"
    path.write_text('{"run_id":"one", "run_id":"two"}')
    with pytest.raises(ValueError, match="Duplicate"):
        read_ownership(tmp_path, recorded_bundle.run_id)
    path.write_bytes(b" " * 1048577)
    with pytest.raises(ValueError, match="bounded"):
        read_ownership(tmp_path, recorded_bundle.run_id)


@pytest.mark.parametrize("component", ["runs", "bundle", "manifest"])
def test_symlinked_ownership_paths_are_refused(tmp_path, recorded_bundle, component):
    source = {
        "runs": tmp_path / "runs",
        "bundle": recorded_bundle.path,
        "manifest": recorded_bundle.path / "manifest.json",
    }[component]
    destination = tmp_path / f"moved-{component}"
    source.rename(destination)
    source.symlink_to(destination, target_is_directory=destination.is_dir())
    with pytest.raises((ValueError, OSError)):
        read_ownership(tmp_path, recorded_bundle.run_id)


def test_fifo_manifest_does_not_block(tmp_path, recorded_bundle):
    path = recorded_bundle.path / "manifest.json"
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        read_ownership(tmp_path, recorded_bundle.run_id)


def test_output_lock_excludes_recovery_and_direct_engine(
    monkeypatch, tmp_path, recorded_bundle, scenario
):
    preflight = Mock(side_effect=AssertionError("busy root must not reach Docker"))
    monkeypatch.setattr("faultline.cli.client_and_preflight", preflight)
    with execution_lock(tmp_path):
        result = CliRunner().invoke(app, ["--output", str(tmp_path), "clean", "--stale"])
        assert result.exit_code == 2
        assert "Another Faultline" in result.output
        with pytest.raises(ActiveRunError):
            run_one(scenario, "correct", tmp_path, client=Mock(), platform_info={"images": {}})
    preflight.assert_not_called()
    with execution_lock(tmp_path):
        pass  # The same root is reusable after releasing the lease.


def test_symlink_lock_is_never_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("preserve")
    (tmp_path / "execution.lock").symlink_to(target)
    with pytest.raises(OSError):
        with execution_lock(tmp_path):
            pytest.fail("unsafe lock accepted")
    assert target.read_text() == "preserve"


def test_copied_bundle_cannot_bypass_original_live_lock(monkeypatch, tmp_path, recorded_bundle):
    copied_root = tmp_path / "copied"
    shutil.copytree(recorded_bundle.path, copied_root / "runs" / recorded_bundle.run_id)
    preflight = Mock(side_effect=AssertionError("copied bundles cannot authorize cleanup"))
    monkeypatch.setattr("faultline.cli.client_and_preflight", preflight)
    with execution_lock(tmp_path):
        result = CliRunner().invoke(
            app, ["--output", str(copied_root), "clean", "--run", recorded_bundle.run_id]
        )
    assert result.exit_code == 2
    assert "Original execution root" in result.output
    preflight.assert_not_called()


@pytest.mark.parametrize("arguments", [[], ["--run", str(uuid4()), "--stale"]])
def test_clean_requires_exactly_one_selector(tmp_path, arguments):
    result = CliRunner().invoke(app, ["--output", str(tmp_path), "clean", *arguments])
    assert result.exit_code == 2
    assert "exactly one" in result.output


def test_empty_stale_selection_does_not_need_docker(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://unavailable:2375")
    result = CliRunner().invoke(
        app, ["--output", str(tmp_path), "--format", "json", "clean", "--stale"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["summary"]["runs_checked"] == 0


def test_stale_scan_is_bounded_before_mutation(monkeypatch, tmp_path, recorded_bundle):
    monkeypatch.setattr("faultline.recovery.MAX_RUNS", 1)
    (tmp_path / "runs" / str(uuid4())).mkdir()
    with pytest.raises(ValueError, match="Too many"):
        recovery_manifests(tmp_path, None)


def test_recovery_rechecks_labels_and_preserves_original_evidence(tmp_path, recorded_bundle):
    original = (recorded_bundle.path / "manifest.json").read_bytes()
    client = Mock()
    # A resource with this ID exists, but its labels do not establish our ownership.
    impostor = Mock(id="a" * 64, attrs={"Config": {"Labels": {}}})
    client.containers.get.return_value = impostor
    client.networks.get.side_effect = NotFound("already absent")
    manifests = recovery_manifests(tmp_path, None)
    with execution_lock(tmp_path):
        results, paths = recover(tmp_path, manifests, client)
    assert not results[0]["complete"]
    assert results[0]["remaining_resources"] == [recorded_bundle.manifest["resources"][0]]
    impostor.remove.assert_not_called()
    client.containers.list.assert_not_called()
    client.networks.list.assert_not_called()
    assert (recorded_bundle.path / "manifest.json").read_bytes() == original
    assert json.loads(paths[0].read_text())["complete"] is False
    # A second recovery may succeed, but it must not erase the original refusal receipt.
    client.containers.get.side_effect = NotFound("now absent")
    with execution_lock(tmp_path):
        again, new_paths = recover(tmp_path, manifests, client)
    assert again[0]["complete"]
    assert paths != new_paths
    assert json.loads(paths[0].read_text())["complete"] is False


def test_no_deletion_if_receipt_cannot_be_written(monkeypatch, tmp_path, recorded_bundle):
    monkeypatch.setattr("faultline.recovery.atomic_json", Mock(side_effect=OSError("disk full")))
    client = Mock()
    with execution_lock(tmp_path), pytest.raises(OSError, match="disk full"):
        recover(tmp_path, [recorded_bundle.manifest], client)
    client.containers.get.assert_not_called()
    client.networks.get.assert_not_called()


def test_invalid_member_prevents_entire_stale_batch(monkeypatch, tmp_path, recorded_bundle):
    other = tmp_path / "runs" / str(uuid4())
    other.mkdir()
    (other / "manifest.json").write_text("{truncated")
    preflight = Mock(side_effect=AssertionError("entire selection must validate first"))
    monkeypatch.setattr("faultline.cli.client_and_preflight", preflight)
    result = CliRunner().invoke(app, ["--output", str(tmp_path), "clean", "--stale"])
    assert result.exit_code == 2
    preflight.assert_not_called()


def test_recovery_operational_failure_has_exit_four(monkeypatch, tmp_path, recorded_bundle):
    client = Mock()
    monkeypatch.setattr("faultline.cli.client_and_preflight", lambda **kw: (client, {}))
    monkeypatch.setattr("faultline.cli.recover", Mock(side_effect=OSError("disk full")))
    result = CliRunner().invoke(
        app, ["--output", str(tmp_path), "--format", "json", "clean", "--stale"]
    )
    assert result.exit_code == 4, result.output
    assert json.loads(result.stdout)["diagnostics"][0]["message"] == "disk full"
    client.close.assert_called_once()
