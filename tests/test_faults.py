from unittest.mock import Mock

import pytest

from faultline.faults import FaultController
from faultline.models import Scenario


def fault_scenario(scenario, kind="proxy_latency"):
    raw = scenario.model_dump(by_alias=True)
    raw["faults"] = [
        {
            "id": "impair",
            "kind": kind,
            "target": "database",
            "trigger": {"offset_ms": 0},
            "latency_ms": 100,
            "duration_ms": 200,
            "direction": "downstream",
        }
    ]
    return Scenario.model_validate(raw)


def test_acknowledged_duration_and_healing(scenario):
    env = Mock(assert_owned=Mock())
    env.containers = {"proxy": Mock(id="owned-proxy")}
    now = [0.0]
    events = []
    applied = set()
    env.call.side_effect = [
        {"acknowledged": True, "action": "latency"},
        {"acknowledged": True, "action": "remove"},
    ]
    controller = FaultController(
        env,
        fault_scenario(scenario),
        lambda kind, payload, **refs: events.append((kind, payload, refs)),
        applied,
        clock=lambda: now[0],
    )
    controller.start()
    controller.tick()
    assert applied == {"impair"} and controller.pending()
    now[0] = 0.199
    controller.tick()
    assert controller.pending()
    now[0] = 0.201
    controller.tick()
    assert not controller.pending()
    assert [e[0] for e in events] == [
        "fault_requested",
        "fault_eligible",
        "fault_applied",
        "fault_healed",
    ]
    env.assert_owned.assert_called()


def test_missing_ack_is_error_not_fault_coverage(scenario):
    env = Mock(assert_owned=Mock())
    env.containers = {"proxy": Mock()}
    env.call.return_value = {"acknowledged": False}
    applied = set()
    controller = FaultController(env, fault_scenario(scenario), Mock(), applied)
    controller.start()
    with pytest.raises(RuntimeError, match="acknowledgment"):
        controller.tick()
    assert not applied and controller.pending()


def test_foreign_resource_is_never_mutated(scenario):
    resource = Mock()
    env = Mock(containers={"proxy": resource}, assert_owned=Mock())
    env.assert_owned.side_effect = RuntimeError("ownership mismatch")
    controller = FaultController(env, fault_scenario(scenario), Mock(), set())
    controller.start()
    with pytest.raises(RuntimeError, match="ownership"):
        controller.tick()
    env.call.assert_not_called()
    resource.kill.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"target": "other-run"},
        {"duration_ms": -1},
        {"direction": "inbound"},
        {"trigger": {"offset_ms": 0, "event": "client_outcome"}},
        {
            "trigger": {
                "event": "upstream_response_complete",
                "operation_id": "purchase_a",
                "attempt": 1,
            }
        },
    ],
)
def test_invalid_fault_contracts_rejected(scenario, change):
    raw = fault_scenario(scenario).model_dump(by_alias=True)
    raw["faults"][0].update(change)
    with pytest.raises(ValueError):
        Scenario.model_validate(raw)


def test_offset_fault_gate_cycle_is_not_invented(scenario):
    raw = fault_scenario(scenario).model_dump(by_alias=True)
    raw["operations"][0]["after_faults"] = ["impair"]
    assert Scenario.model_validate(raw)


def test_fault_gate_on_triggering_attempt_is_rejected(scenario):
    raw = fault_scenario(scenario).model_dump(by_alias=True)
    raw["faults"][0]["trigger"] = {
        "event": "attempt_dispatched",
        "operation_id": "purchase_a",
        "attempt": 1,
    }
    raw["operations"][0]["after_faults"] = ["impair"]
    with pytest.raises(ValueError, match="cyclic"):
        Scenario.model_validate(raw)
