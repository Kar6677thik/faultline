from copy import deepcopy

import pytest
import yaml
from pydantic import ValidationError

from faultline.adapters import AdapterError, bundled_adapter
from faultline.models import Scenario, load_scenario


@pytest.mark.parametrize(
    "document",
    [
        "schema_version: 1\nschema_version: 1\n",
        "thing: &anchor {x: 1}\nother: *anchor\n",
        "thing: !!python/object:object {}\n",
        "thing: " + "[" * 33 + "1" + "]" * 33,
    ],
)
def test_unsafe_yaml_is_rejected(tmp_path, document):
    path = tmp_path / "bad.yaml"
    path.write_text(document)
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_scenario(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema_version=True),
        lambda s: s.update(concurrency=True),
        lambda s: s.update(concurrency=33),
        lambda s: s.update(unknown_field=True),
        lambda s: s.update(seed="42"),
        lambda s: s["operations"][0]["request"].update(path="https://production.example/orders"),
        lambda s: s["operations"][0]["request"]["headers"].update(Authorization="secret"),
        lambda s: s["faults"][0]["trigger"].update(attempt=3),
        lambda s: s["faults"][0]["trigger"].update(operation_id="absent"),
        lambda s: s["operations"][0]["retry"].update(backoff_ms=[]),
        lambda s: s["operations"][0].update(logical_id="different"),
    ],
)
def test_unsupported_or_ambiguous_plans_rejected(scenario, mutate):
    data = deepcopy(scenario.model_dump(by_alias=True))
    mutate(data)
    with pytest.raises((ValidationError, AdapterError, ValueError)):
        bundled_adapter().validate(Scenario.model_validate(data))


def test_yaml_on_is_a_string_key_and_plan_is_stable(scenario):
    assert scenario.operations[0].retry.on == ["timeout", "connection_error"]
    assert scenario.plan() == Scenario.model_validate(scenario.model_dump(by_alias=True)).plan()
