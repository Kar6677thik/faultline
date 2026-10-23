from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from faultline.artifacts import read_bundle
from faultline.engine import run_one
from faultline.environment import Environment


def test_mutation_requires_both_manifest_and_labels():
    bundle = SimpleNamespace(
        run_id="our-run", manifest={"ownership_nonce": "nonce", "resources": []}
    )
    env = Environment(Mock(), bundle, {})
    resource = Mock(id="container-id", attrs={"Config": {"Labels": env.labels}})
    with pytest.raises(RuntimeError, match="manifest"):
        env.assert_owned(resource)
    bundle.manifest["resources"].append({"id": resource.id})
    env.assert_owned(resource)
    resource.attrs["Config"]["Labels"] = dict(env.labels, **{"io.faultline.run": "somebody-else"})
    with pytest.raises(RuntimeError, match="ownership"):
        env.assert_owned(resource)


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_setup_failure_still_reports_and_cleans(monkeypatch, tmp_path, scenario, cleanup_ok):
    environment = Mock()
    environment.provision.side_effect = RuntimeError("simulated setup failure")
    environment.cleanup.return_value = {"complete": cleanup_ok, "remaining_resources": []}
    monkeypatch.setattr("faultline.engine.Environment", lambda *args: environment)
    path, report = run_one(
        scenario, "unsafe", tmp_path, client=Mock(), platform_info={"images": {}}
    )
    environment.cleanup.assert_called_once()
    assert report["verdict"] == "ERROR"
    assert report["cleanup"]["complete"] == cleanup_ok
    assert read_bundle(path)[1]["verdict"] == "ERROR"


def test_keyboard_interrupt_cleans_and_is_not_pass(monkeypatch, tmp_path, scenario):
    environment = Mock()
    environment.provision.side_effect = KeyboardInterrupt()
    environment.cleanup.return_value = {"complete": True, "remaining_resources": []}
    monkeypatch.setattr("faultline.engine.Environment", lambda *args: environment)
    _path, report = run_one(
        scenario, "correct", tmp_path, client=Mock(), platform_info={"images": {}}
    )
    environment.cleanup.assert_called_once()
    assert report["lifecycle"] == "interrupted"
    assert report["verdict"] == "INCONCLUSIVE"
