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


def test_drain_helpers_share_the_stage_budget_instead_of_fixed_timeouts():
    from unittest.mock import Mock

    from faultline.environment import Environment

    env = object.__new__(Environment)
    app = Mock(attrs={"State": {"Running": False, "ExitCode": 0}})
    env.containers = {"application": app}
    env.assert_owned = Mock()
    env.bundle = Mock()
    replies = iter(
        [
            {"registrations": [], "application_sessions": 1},
            {"registrations": [], "application_sessions": 0},
            {"cleared": []},
        ]
    )
    env.call = Mock(side_effect=lambda *a, **k: next(replies))
    assert env.drain(30000) is True
    timeouts = [call.kwargs["timeout"] for call in env.call.call_args_list]
    # A slow helper start-up on a busy host no longer hits a hard-coded 5 s limit.
    assert all(5 < value <= 30 for value in timeouts)
    # The graceful stop may use at most half of what remains.
    assert app.stop.call_args.kwargs["timeout"] <= 15
