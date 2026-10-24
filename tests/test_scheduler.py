"""Observe admission and effects, rather than assuming threads imply concurrency."""

import json
import subprocess
import sys
import threading
import time
from copy import deepcopy

import pytest

from faultline.models import Scenario, canonical_bytes
from faultline.scheduler import execute_attempts
from faultline.verifier import EvidenceError, verify


def multiple(scenario, count=3, concurrency=2):
    raw = scenario.model_dump(by_alias=True)
    raw.update(faults=[], concurrency=concurrency)
    first = raw["operations"][0]
    first["retry"] = {"max_attempts": 1, "on": [], "backoff_ms": []}
    raw["operations"] = []
    for i in range(count):
        op = deepcopy(first)
        op["id"] = f"op_{i}"
        raw["operations"].append(op)
    return raw


def test_barrier_admission_is_bounded_and_dependency_waits_for_every_retry(scenario):
    raw = multiple(scenario)
    raw["barriers"] = [{"id": "together", "members": ["op_0", "op_1"]}]
    for op in raw["operations"][:2]:
        op["barrier"] = "together"
    raw["operations"][0]["retry"] = {"max_attempts": 2, "on": ["http_503"], "backoff_ms": [5]}
    raw["operations"][2]["after"] = ["op_0", "op_1"]
    events, requests = [], []
    overlap = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = 0

    def transport(request):
        nonlocal active, peak
        identity = request["headers"]["X-Faultline-Attempt-Id"]
        with lock:
            active += 1
            peak = max(peak, active)
            requests.append(request)
        if identity in {"op_0/1", "op_1/1"}:
            overlap.wait(timeout=2)
        with lock:
            active -= 1
        return {"outcome": "http_response", "status": 503 if identity == "op_0/1" else 201}

    attempts, _, complete = execute_attempts(
        Scenario.model_validate(raw),
        transport,
        lambda kind, payload, **refs: events.append((kind, payload, refs)),
        "run",
    )
    assert complete and peak == 2 and len(attempts) == 4
    admitted = [e[2]["attempt_id"] for e in events if e[0] == "attempt_dispatched"]
    assert admitted == ["op_0/1", "op_1/1", "op_0/2", "op_2/1"]
    assert events[0][0] == "barrier_released"
    first, retry = [r for r in requests if r["headers"]["X-Faultline-Operation-Id"] == "op_0"]
    assert first["body"] == retry["body"]
    assert first["headers"]["Idempotency-Key"] == retry["headers"]["Idempotency-Key"]


def test_backoff_does_not_block_independent_operation(scenario):
    raw = multiple(scenario, count=2, concurrency=1)
    raw["operations"][0]["retry"] = {"max_attempts": 2, "on": ["timeout"], "backoff_ms": [30]}
    dispatched = []

    def transport(request):
        identity = request["headers"]["X-Faultline-Attempt-Id"]
        dispatched.append(identity)
        return (
            {"outcome": "timeout"}
            if identity == "op_0/1"
            else {"outcome": "http_response", "status": 201}
        )

    execute_attempts(Scenario.model_validate(raw), transport, lambda *a, **kw: None, "run")
    assert dispatched == ["op_0/1", "op_1/1", "op_0/2"]


def test_missed_contingent_fault_gate_is_incomplete_without_dispatch(scenario):
    raw = multiple(scenario, count=2)
    raw["faults"] = scenario.model_dump(by_alias=True)["faults"]
    raw["faults"][0]["trigger"]["operation_id"] = "op_0"
    raw["operations"][1]["after_faults"] = [raw["faults"][0]["id"]]
    attempts, applied, complete = execute_attempts(
        Scenario.model_validate(raw),
        lambda _: {"outcome": "connection_error"},
        lambda *a, **kw: None,
        "run",
    )
    assert not complete and not applied
    assert [a["attempt_id"] for a in attempts] == ["op_0/1"]


@pytest.mark.parametrize(
    "case", ["cycle", "barrier_cycle", "fault_cycle", "membership", "duplicate"]
)
def test_invalid_dependency_graph_is_rejected(scenario, case):
    raw = multiple(scenario, count=2)
    if case == "cycle":
        raw["operations"][0]["after"] = ["op_1"]
        raw["operations"][1]["after"] = ["op_0"]
    elif case in {"barrier_cycle", "membership"}:
        raw["barriers"] = [{"id": "b", "members": ["op_0", "op_1"]}]
        raw["operations"][0]["barrier"] = "b"
        if case == "barrier_cycle":
            raw["operations"][1]["barrier"] = "b"
            raw["operations"][0]["after"] = ["op_1"]
    elif case == "fault_cycle":
        raw["faults"] = scenario.model_dump(by_alias=True)["faults"]
        raw["faults"][0]["trigger"]["operation_id"] = "op_0"
        raw["operations"][0]["after_faults"] = [raw["faults"][0]["id"]]
    else:
        raw["operations"][1]["id"] = "op_0"
    with pytest.raises(ValueError):
        Scenario.model_validate(raw)


def test_generator_materialization_is_identical_across_processes(scenario):
    raw = multiple(scenario, count=0)
    raw["generators"] = [
        {
            "id": "generated",
            "kind": "orders.purchases",
            "count": 5,
            "parameters": {"quantity_min": 1, "quantity_max": 3},
        }
    ]
    code = (
        "import json,sys; from faultline.models import Scenario,canonical_bytes; "
        "sys.stdout.buffer.write(canonical_bytes(Scenario.model_validate(json.load(sys.stdin)).plan()))"
    )
    plans = [
        subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps(raw).encode(),
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        for _ in range(3)
    ]
    assert plans[0] == plans[1] == plans[2]
    plan = json.loads(plans[0])
    assert len(plan["requests"]) == 5 and not plan["scenario"]["generators"]
    # Saved execution inputs require no generator to recover their values.
    restored = Scenario.model_validate(plan["scenario"])
    assert canonical_bytes(
        [op.model_dump(by_alias=True) for op in restored.all_operations]
    ) == canonical_bytes(plan["scenario"]["operations"])


def test_worker_exception_propagates_without_more_admission(scenario):
    raw = multiple(scenario, concurrency=1)
    calls = []

    def broken(request):
        calls.append(request)
        raise RuntimeError("worker stream lost")

    with pytest.raises(RuntimeError, match="stream lost"):
        execute_attempts(Scenario.model_validate(raw), broken, lambda *a, **kw: None, "run")
    assert len(calls) == 1


def test_unknown_attempt_is_not_silently_ignored(scenario, baseline, correct_snapshot):
    with pytest.raises(EvidenceError, match="attempt identity"):
        verify(scenario, baseline, correct_snapshot, [{"attempt_id": "unknown/1"}])


def test_slow_transport_does_not_serialize_other_slots(scenario):
    raw = multiple(scenario, concurrency=2)
    later_finished = threading.Event()

    def transport(request):
        identity = request["headers"]["X-Faultline-Operation-Id"]
        if identity == "op_0":
            assert later_finished.wait(timeout=2)
        if identity == "op_2":
            later_finished.set()
        return {"outcome": "http_response", "status": 201}

    started = time.monotonic()
    result = execute_attempts(Scenario.model_validate(raw), transport, lambda *a, **kw: None, "run")
    assert result[2] and time.monotonic() - started < 2
