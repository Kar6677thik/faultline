"""Registered generators must produce checked inputs before Docker admission."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from faultline.adapters import AdapterError, CheckedAdapter
from faultline.engine import run_one
from faultline.models import Scenario, canonical_bytes, load_document, safe_path
from faultline.projects import Project

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/counter-adapter"


@pytest.fixture
def counter(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE / "src"))
    from faultline_counter import CounterAdapter

    return CounterAdapter()


def generated():
    return Scenario.model_validate(load_document(EXAMPLE / "generated.yaml"))


def test_custom_generator_materializes_identically_in_separate_processes():
    code = (
        "import json,sys; from faultline.models import Scenario,canonical_bytes; "
        "from faultline.adapters import CheckedAdapter; "
        "from faultline_counter import CounterAdapter; "
        "s=Scenario.model_validate(json.load(sys.stdin)); "
        "CheckedAdapter(CounterAdapter()).validate(s); "
        "sys.stdout.buffer.write(canonical_bytes(s.plan()))"
    )
    plans = [
        subprocess.run(
            [sys.executable, "-c", code],
            input=canonical_bytes(load_document(EXAMPLE / "generated.yaml")),
            capture_output=True,
            check=True,
            timeout=10,
            env=dict(os.environ, PYTHONPATH=str(EXAMPLE / "src")),
        ).stdout
        for _ in range(3)
    ]
    assert plans[0] == plans[1] == plans[2]
    plan = json.loads(plans[0])
    assert plan["adapter_contract"]["generators"] == {"counter.increments": 1}
    assert len(plan["scenario"]["operations"]) == 3
    restored = Scenario.model_validate(plan["scenario"])
    assert [op.model_dump(by_alias=True) for op in restored.all_operations] == plan["scenario"][
        "operations"
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(version=2),
        lambda v: v.update(version=True),
        lambda v: v.update(algorithm=""),
        lambda v: v["operations"].pop(),
        lambda v: v.update(extra="unrecognized"),
        lambda v: v["operations"][0]["request"].update(path="https://production.example/"),
    ],
)
def test_malformed_expansion_is_a_contract_error(counter, change):
    scenario = generated()
    value = counter.expand(scenario.generators[0].model_dump(), scenario.seed)
    change(value)
    counter.expand = Mock(return_value=value)
    with pytest.raises(AdapterError) as error:
        CheckedAdapter(counter).validate(scenario)
    assert error.value.hook == "expand"


@pytest.mark.parametrize("case", ["duplicate", "cycle", "attempt_limit"])
def test_expansion_cannot_bypass_graph_or_project_limits(counter, case):
    project_raw = load_document(EXAMPLE / "faultline.project.yaml")
    project_raw["limits"]["max_attempts"] = 3
    scenario = Project.model_validate(project_raw).scenario(
        load_document(EXAMPLE / "generated.yaml")
    )
    value = counter.expand(scenario.generators[0].model_dump(), scenario.seed)
    first, second = value["operations"][:2]
    if case == "duplicate":
        second["id"] = first["id"]
    elif case == "cycle":
        first["after"], second["after"] = [second["id"]], [first["id"]]
    else:
        first["retry"] = {"max_attempts": 2, "on": ["timeout"], "backoff_ms": [100]}
    counter.expand = Mock(return_value=value)
    with pytest.raises(ValueError, match="duplicate|cyclic|attempt count"):
        CheckedAdapter(counter).validate(scenario)


def test_unknown_generator_is_rejected_before_docker(monkeypatch, tmp_path):
    preflight = Mock(side_effect=AssertionError("Docker must not be contacted"))
    monkeypatch.setattr("faultline.engine.client_and_preflight", preflight)
    with pytest.raises(ValueError, match="unregistered generator"):
        run_one(generated(), "correct", tmp_path / "output")
    preflight.assert_not_called()
    assert not (tmp_path / "output").exists()


def test_counter_rejects_unimplemented_expectations(counter):
    raw = load_document(EXAMPLE / "lost-response.yaml")
    raw["operations"][0]["expectation"] = "insufficient_funds"
    with pytest.raises(AdapterError):
        CheckedAdapter(counter).validate(Scenario.model_validate(raw))


def test_project_routes_are_multiple_but_unambiguous():
    raw = load_document(EXAMPLE / "faultline.project.yaml")
    raw["services"][0]["ports"].append(8001)
    raw["routes"].append(
        {"id": "second_api", "service": "application", "port": 8001, "base_path": "/v2"}
    )
    assert len(Project.model_validate(raw).routes) == 2
    raw["routes"].append(deepcopy(raw["routes"][0]))
    with pytest.raises(ValueError, match="duplicate route"):
        Project.model_validate(raw)


@pytest.mark.parametrize("path", ["/x?key=a&key=b", "/x?key=a&%6bey=b"])
def test_repeated_query_keys_are_rejected(path):
    with pytest.raises(ValueError, match="repeated query"):
        safe_path(path)
