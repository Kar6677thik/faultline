import signal
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from faultline.artifacts import read_bundle
from faultline.deadlines import DeadlineExceeded, RunDeadlines, alarm_until, check_alarm_available
from faultline.engine import run_one
from faultline.locking import execution_lock
from faultline.models import Limits, Scenario


@pytest.mark.parametrize(
    "overrides",
    [
        {"provisioning_ms": 0},
        {"observation_ms": True},
        {"verification_ms": 300001},
        {"cleanup_ms": "30000"},
        {"total_ms": 900001},
        {"total_ms": 30000, "cleanup_ms": 30000},
        {"total_ms": 100, "cleanup_ms": 200},
    ],
)
def test_invalid_deadline_contracts(overrides):
    with pytest.raises(ValidationError):
        Limits(**overrides)


def test_plan_records_all_effective_deadline_defaults(scenario):
    limits = scenario.plan()["scenario"]["limits"]
    assert limits["provisioning_ms"] == 60000
    assert limits["execution_ms"] == 60000
    assert limits["observation_ms"] == limits["verification_ms"] == 10000
    assert limits["cleanup_ms"] == 30000
    assert limits["total_ms"] == 300000


def test_alarm_interrupts_blocking_wait_and_restores_handler():
    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded) as error:
        with alarm_until("provisioning", started + 0.05, "stage"):
            try:
                time.sleep(2)
            except Exception:
                pytest.fail("ordinary transport exception handling swallowed the stage alarm")
    assert error.value.stage == "provisioning"
    assert time.monotonic() - started < 1
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_existing_alarm_is_not_replaced():
    previous = signal.getsignal(signal.SIGALRM)
    signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        with pytest.raises(ValueError, match="existing process alarm"):
            check_alarm_available()
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 20
        assert signal.getsignal(signal.SIGALRM) == previous
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_non_main_thread_is_refused():
    results = []

    def attempt():
        try:
            check_alarm_available()
        except ValueError as exc:
            results.append(str(exc))

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert results and "main thread" in results[0]


def test_exhausted_total_work_budget_does_not_enter_another_stage():
    budgets = RunDeadlines(Limits(total_ms=1100, cleanup_ms=1000))
    budgets.work_end = time.monotonic() - 1
    with pytest.raises(DeadlineExceeded) as error:
        with budgets.stage("observing", 10000):
            pytest.fail("new work must not consume the cleanup reserve")
    assert error.value.scope == "total_work"
    cleanup_end, total_end = budgets.cleanup_windows()
    assert time.monotonic() < cleanup_end < total_end <= budgets.total_end


@pytest.fixture
def environment(monkeypatch, baseline, correct_snapshot):
    env = Mock()
    env.cleanup_remaining = []
    env.snapshot.side_effect = lambda role: deepcopy(
        baseline if role == "baseline" else correct_snapshot
    )
    env.drain.return_value = True
    response = {
        "outcome": "http_response",
        "status": 201,
        "body": correct_snapshot["records"]["orders"][0],
    }
    env.call.return_value = response
    env.call.side_effect = lambda role, *args, **kwargs: (
        {"prepared": True} if role == "observer" else response
    )
    env.cleanup.return_value = {"complete": True, "remaining_resources": []}
    monkeypatch.setattr("faultline.engine.Environment", lambda *args: env)
    return env


def scenario_with(scenario, **limits):
    raw = scenario.model_dump(by_alias=True)
    raw["faults"] = []
    raw["limits"].update(limits)
    return Scenario.model_validate(raw)


def run_fixture(tmp_path, scenario):
    return run_one(scenario, "correct", tmp_path, client=Mock(), platform_info={"images": {}})


@pytest.mark.parametrize("stage", ["provisioning", "observing", "verifying"])
def test_stage_timeouts_are_harness_errors_and_still_cleanup(
    monkeypatch, tmp_path, scenario, environment, stage
):
    limits = {
        "provisioning": "provisioning_ms",
        "observing": "observation_ms",
        "verifying": "verification_ms",
    }
    value = scenario_with(scenario, **{limits[stage]: 100})
    if stage == "provisioning":
        environment.provision.side_effect = lambda: time.sleep(2)
    elif stage == "observing":
        snapshot = environment.snapshot.side_effect
        environment.snapshot.side_effect = lambda role: (
            time.sleep(2) if role == "final" else snapshot(role)
        )
    else:

        def slow_verifier(*args, **kwargs):
            # CPU-bound grading must be interruptible, not merely socket reads.
            finish = time.monotonic() + 2
            while time.monotonic() < finish:
                pass

        monkeypatch.setattr("faultline.adapters.verifier.verify", slow_verifier)
    path, report = run_fixture(tmp_path, value)
    assert report["verdict"] == "ERROR"
    assert any(d.get("stage") == stage for d in report["diagnostics"])
    assert report["cleanup"]["complete"]
    environment.cleanup.assert_called_once()
    assert read_bundle(path)[1]["verdict"] == "ERROR"
    assert any(s["stage"] == stage and s["expired"] for s in report["timing"]["stages"])


def test_execution_timeout_retains_fault_and_attempt_evidence(tmp_path, scenario, environment):
    raw = scenario.model_dump(by_alias=True)
    raw["operations"][0]["timeout_ms"] = 50
    raw["operations"][0]["retry"]["backoff_ms"] = [0]
    raw["limits"]["execution_ms"] = 100
    response = environment.call.return_value
    calls = 0

    def transport(*args, **kwargs):
        nonlocal calls
        if args[0] == "observer":
            return {"prepared": True}
        calls += 1
        if calls > 1:
            time.sleep(2)
        return response

    environment.call.side_effect = transport
    _path, report = run_fixture(tmp_path, Scenario.model_validate(raw))
    assert calls == 2
    assert report["verdict"] == "INCONCLUSIVE"
    assert not report["coverage"]["workload_complete"]
    assert report["coverage"]["required_applied"] == 1
    assert len(report["attempts"]) == 1
    assert report["attempts"][0]["cause"] == "injected_response_suppression"
    assert report["cleanup"]["complete"]


def test_drain_timeout_cannot_claim_certain_final_state(tmp_path, scenario, environment):
    environment.drain.side_effect = lambda _: time.sleep(2)
    _path, report = run_fixture(tmp_path, scenario_with(scenario, drain_ms=50))
    assert report["verdict"] == "INCONCLUSIVE"
    assert any(finding["status"] == "UNKNOWN" for finding in report["invariants"])
    assert report["cleanup"]["complete"]


def test_drain_returning_success_at_deadline_is_still_uncertain(
    monkeypatch, tmp_path, scenario, environment
):
    original = RunDeadlines.stage

    @contextmanager
    def deadline_on_exit(self, name, milliseconds, **kwargs):
        with original(self, name, milliseconds, **kwargs):
            yield
        if name == "draining":
            # Models an alarm delivered after drain returned True but before stage exit.
            raise DeadlineExceeded(name, "stage")

    monkeypatch.setattr(RunDeadlines, "stage", deadline_on_exit)
    _path, report = run_fixture(tmp_path, scenario_with(scenario))
    assert environment.drain.return_value is True
    assert report["verdict"] == "INCONCLUSIVE"
    assert any(f["status"] == "UNKNOWN" for f in report["invariants"])


def test_total_budget_preserves_cleanup_time(tmp_path, scenario, environment):
    environment.provision.side_effect = lambda: time.sleep(2)
    started = time.monotonic()
    _path, report = run_fixture(tmp_path, scenario_with(scenario, total_ms=500, cleanup_ms=300))
    assert time.monotonic() - started < 1.5
    assert report["verdict"] == "ERROR"
    assert any(d.get("scope") == "total_work" for d in report["diagnostics"])
    assert report["cleanup"]["complete"]
    environment.cleanup.assert_called_once()


def test_cleanup_timeout_retains_unresolved_ids_and_verified_findings(
    tmp_path, scenario, environment
):
    unresolved = [{"kind": "container", "id": "a" * 64, "role": "application"}]
    environment.cleanup_remaining = unresolved
    environment.cleanup.side_effect = lambda: time.sleep(2)
    path, report = run_fixture(tmp_path, scenario_with(scenario, cleanup_ms=400))
    assert report["verdict"] == "ERROR"
    assert report["invariants"] and all(f["status"] == "PASS" for f in report["invariants"])
    assert not report["cleanup"]["complete"]
    assert report["cleanup"]["remaining_resources"] == unresolved
    assert any(d.get("stage") == "cleanup" for d in report["diagnostics"])
    assert read_bundle(path)[1]["cleanup"]["remaining_resources"] == unresolved


def test_finalization_timeout_leaves_partial_bundle_and_releases_lock(
    monkeypatch, tmp_path, scenario, environment
):
    monkeypatch.setattr("faultline.engine.finalize_run", lambda *args: time.sleep(2))
    with pytest.raises(RuntimeError, match="finalizing.*partial evidence"):
        run_fixture(tmp_path, scenario_with(scenario, cleanup_ms=200))
    assert list((tmp_path / "runs").glob("*/manifest.json"))
    assert not list((tmp_path / "runs").glob("*/report.json"))
    environment.cleanup.assert_called_once()
    with execution_lock(tmp_path):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
