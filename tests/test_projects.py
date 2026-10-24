"""Project boundaries must constrain execution, not merely accept a configuration file."""

import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from faultline.artifacts import Bundle, read_bundle
from faultline.cli import app
from faultline.engine import run_one
from faultline.environment import Environment, PreflightError, client_and_preflight
from faultline.models import digest, load_document
from faultline.projects import Project, load_project
from faultline.recovery import read_ownership

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_document():
    return load_document(ROOT / "examples/orders/faultline.project.yaml")


@pytest.fixture
def local_project(tmp_path, project_document):
    for filename in project_document["scenario_paths"]:
        (tmp_path / filename).write_bytes((ROOT / "examples/orders" / filename).read_bytes())
    path = tmp_path / "faultline.project.yaml"
    path.write_text(json.dumps(project_document))
    return path


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(project_id="production/../other"),
        lambda p: p["adapter"].update(entry_point="untrusted.module:main"),
        lambda p: p["adapter"].update(contract_version=True),
        lambda p: p["adapter"].update(package_version="0.0.0"),
        lambda p: p["services"].append(deepcopy(p["services"][0])),
        lambda p: p["services"][1].update(id="application"),
        lambda p: p["services"][1].update(image="https://remote.example/postgres"),
        lambda p: p["services"][2].update(image="unversioned-helper"),
        lambda p: p["services"][0].update(privileged=True),
        lambda p: p["services"][0].update(network_mode="host"),
        lambda p: p["services"][0].update(volumes=["/:/host"]),
        lambda p: p["services"][0].update(writable_paths=["/var/run/docker.sock"]),
        lambda p: p["services"][0].update(environment={"FL_APP_PASSWORD": "secret-token"}),
        lambda p: p["services"][0]["readiness"].update(command="touch /host/file"),
        lambda p: p["services"][0]["readiness"].update(timeout_ms=0),
        lambda p: p["services"][0]["resources"].update(memory_bytes=2**30),
        lambda p: p["services"][0]["resources"].update(cpu_millis="500"),
        lambda p: p["services"][0]["resources"].update(pids_limit=True),
        lambda p: p["routes"][0].update(base_path="https://production.example/"),
        lambda p: p["routes"][0].update(base_path="/%2e%2e/"),
        lambda p: p["routes"][0].update(service="database"),
        lambda p: p["routes"][0].update(port=5432),
        lambda p: p["routes"].append(deepcopy(p["routes"][0])),
        lambda p: p["variants"]["correct"].update(application_image="https://host/image:1"),
        lambda p: p["variants"]["correct"].update(application_image="untagged"),
        lambda p: p["limits"].update(total_ms=None),
        lambda p: p["limits"].update(max_operations=1001),
        lambda p: p["scenario_paths"].append(p["scenario_paths"][0]),
    ],
)
def test_unsafe_or_unsupported_project_rejected_before_docker(
    monkeypatch, local_project, project_document, mutate
):
    mutate(project_document)
    local_project.write_text(json.dumps(project_document))
    preflight = Mock(side_effect=AssertionError("must not reach Docker"))
    monkeypatch.setattr("faultline.cli.client_and_preflight", preflight)
    result = CliRunner().invoke(
        app,
        [
            "--project",
            str(local_project),
            "--format",
            "json",
            "run",
            "control.yaml",
            "--variant",
            "correct",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "secret-token" not in result.stdout
    preflight.assert_not_called()


def test_shared_loader_rejects_ambiguity_and_non_json_values(local_project):
    documents = [
        "schema_version: 1\nschema_version: 1\n",
        "x: &x [1]\ny: *x\n",
        "x: .nan\n",
        "x: 2026-10-23\n",
        "x: !!python/object:object {}\n",
        "x: " + "[" * 33 + "0" + "]" * 33,
    ]
    for document in documents:
        local_project.write_text(document)
        with pytest.raises(ValueError):
            load_project(local_project)


@pytest.mark.parametrize(
    "name", ["../outside.yaml", "/tmp/scenario.yaml", "C:/scenario.yaml", "a//b", "a/./b", "a\\b"]
)
def test_selected_scenario_and_discovery_paths_are_confined(local_project, project_document, name):
    loaded = load_project(local_project)
    with pytest.raises(ValueError):
        loaded.load_scenario(name)
    project_document["scenario_paths"] = [name]
    with pytest.raises(ValueError):
        Project.model_validate(project_document)


def test_symlink_escape_is_rejected_for_selected_and_discovered_paths(local_project, tmp_path):
    (tmp_path / "redirect").symlink_to(ROOT / "examples/orders", target_is_directory=True)
    loaded = load_project(local_project)
    with pytest.raises(ValueError, match="symlinks"):
        loaded.load_scenario("redirect/control.yaml")
    raw = load_document(local_project)
    raw["scenario_paths"] = ["redirect/control.yaml"]
    local_project.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="symlinks"):
        load_project(local_project)


def test_fifo_configuration_is_refused_without_blocking(tmp_path):
    path = tmp_path / "pipe.yaml"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        load_document(path)


def test_project_clips_only_omitted_defaults_and_records_effective_plan(project_document):
    project_document["limits"].update(total_ms=60000, cleanup_ms=10000, execution_ms=4000)
    project = Project.model_validate(project_document)
    raw = load_document(ROOT / "examples/orders/control.yaml")
    scenario = project.scenario(raw)
    assert scenario.limits.total_ms == 60000
    assert scenario.limits.cleanup_ms == 10000
    assert scenario.limits.execution_ms == 4000
    assert scenario.plan()["scenario"]["limits"] == scenario.limits.model_dump()
    raw["limits"] = {"execution_ms": 5000}
    with pytest.raises(ValueError, match="exceeds the project ceiling"):
        project.scenario(raw)


def test_omitted_project_ceiling_uses_engine_cap_not_scenario_default(project_document):
    project_document["limits"] = {}
    raw = load_document(ROOT / "examples/orders/control.yaml")
    raw["limits"] = {"execution_ms": 70000, "provisioning_ms": 80000}
    scenario = Project.model_validate(project_document).scenario(raw)
    assert scenario.limits.execution_ms == 70000
    assert scenario.limits.provisioning_ms == 80000


@pytest.mark.parametrize(
    "ceilings", [{"execution_ms": 500}, {"total_ms": 30000}, {"max_attempts": 1}, {"max_faults": 0}]
)
def test_effective_budget_and_count_contradictions_are_not_silently_repaired(
    project_document, ceilings
):
    project_document["limits"] = ceilings
    raw = load_document(ROOT / "examples/orders/lost-response.yaml")
    with pytest.raises(ValueError):
        Project.model_validate(project_document).scenario(raw)


def test_same_project_and_scenario_have_stable_identity_from_another_cwd(
    monkeypatch, local_project, tmp_path
):
    first = load_project(local_project)
    before = digest(first.load_scenario("control.yaml").plan())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    second = load_project(local_project)
    assert first.project.identity() == second.project.identity()
    assert before == digest(second.load_scenario("control.yaml").plan())


def test_default_project_discovery_and_offline_validate(monkeypatch, local_project):
    monkeypatch.chdir(local_project.parent)
    monkeypatch.setenv("DOCKER_HOST", "tcp://unavailable:2375")
    result = CliRunner().invoke(app, ["--format", "json", "validate", "control.yaml"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)["summary"]
    assert summary["project"]["project_id"] == "orders-example"
    assert summary["effective_limits"]["total_ms"] == 90000
    # An existing bad default manifest must not fall back to implicit configuration.
    local_project.write_text("not: a project")
    assert CliRunner().invoke(app, ["validate", "control.yaml"]).exit_code == 2


def test_single_project_variant_is_selected_but_ambiguous_or_absent_names_refused(project_document):
    project = Project.model_validate(project_document)
    with pytest.raises(ValueError, match="choose --variant"):
        project.select_variant(None)
    del project_document["variants"]["unsafe"]
    project = Project.model_validate(project_document)
    assert project.select_variant(None) == "correct"
    with pytest.raises(ValueError):
        project.select_variant("unsafe")


def test_direct_engine_cannot_bypass_project_ceilings(
    monkeypatch, tmp_path, project_document, scenario
):
    project_document["limits"]["max_attempts"] = 1
    project = Project.model_validate(project_document)
    preflight = Mock()
    monkeypatch.setattr("faultline.engine.client_and_preflight", preflight)
    with pytest.raises(ValueError, match="count exceeds"):
        run_one(scenario, "correct", tmp_path / "output", project=project)
    preflight.assert_not_called()
    assert not (tmp_path / "output").exists()


@pytest.fixture
def daemon(monkeypatch):
    client = Mock()
    client.info.return_value = {
        "OSType": "linux",
        "Architecture": "x86_64",
        "MemTotal": 2**33,
        "NCPU": 4,
    }
    client.version.return_value = {"Version": "test-daemon"}
    client.images.get.side_effect = lambda reference: Mock(
        id="sha256:" + digest(reference), attrs={"Os": "linux", "Architecture": "amd64"}
    )
    monkeypatch.setattr("faultline.environment.docker.DockerClient", lambda **kwargs: client)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    return client


def test_application_image_is_resolved_locally_and_pinned(project_document, daemon):
    project_document["variants"]["correct"]["application_image"] = "my-orders:review-1"
    project = Project.model_validate(project_document)
    client, info = client_and_preflight(project=project, variant="correct")
    assert client is daemon
    assert info["images"]["application"] == "sha256:" + digest("my-orders:review-1")
    assert info["project_configuration_hash"] == project.identity()["configuration_hash"]
    daemon.images.pull.assert_not_called()
    daemon.containers.create.assert_not_called()


@pytest.mark.parametrize("missing_capacity", [{"MemTotal": 1}, {"NCPU": 1}])
def test_project_resource_budget_refused_before_image_or_container_work(
    project_document, daemon, missing_capacity
):
    daemon.info.return_value.update(missing_capacity)
    with pytest.raises(PreflightError, match="host budget"):
        client_and_preflight(project=Project.model_validate(project_document), variant="correct")
    daemon.images.get.assert_not_called()
    daemon.containers.create.assert_not_called()
    daemon.close.assert_called_once()


def test_wrong_image_architecture_is_refused_and_client_closed(project_document, daemon):
    daemon.images.get.side_effect = None
    daemon.images.get.return_value = Mock(attrs={"Os": "linux", "Architecture": "arm64"})
    with pytest.raises(PreflightError, match="images must be Linux"):
        client_and_preflight(project=Project.model_validate(project_document), variant="correct")
    daemon.close.assert_called_once()


def test_resource_settings_and_project_label_reach_docker(tmp_path, project_document, scenario):
    project_document["services"][0]["resources"] = {
        "memory_bytes": 201326592,
        "cpu_millis": 500,
        "pids_limit": 48,
    }
    project = Project.model_validate(project_document)
    bundle = Bundle(tmp_path, scenario, "correct", project.configuration())
    client = Mock()
    env = Environment(client, bundle, {"runtime": "helper-id", "application": "app-id"}, project)
    env.networks = {"client": Mock(id="client-id")}
    container = Mock(id="a" * 64, attrs={"Config": {"Labels": env.labels}})

    def create(*args, **kwargs):
        container.attrs = {"Name": "/" + kwargs["name"], "Config": {"Labels": kwargs["labels"]}}
        return container

    client.containers.create.side_effect = create
    env.create_container("application", ["client"])
    assert client.containers.create.call_args.args == ("app-id",)
    settings = client.containers.create.call_args.kwargs
    assert settings["mem_limit"] == 201326592
    assert settings["nano_cpus"] == 500000000
    assert settings["pids_limit"] == 48
    assert settings["labels"]["io.faultline.project"] == "orders-example"
    assert settings["read_only"]
    assert settings["cap_drop"] == ["ALL"]
    assert "ports" not in settings and "volumes" not in settings
    container.start.assert_called_once()
    # Recovery uses only the saved manifest; changing/removing the YAML is irrelevant.
    recovered_manifest = read_ownership(tmp_path, bundle.run_id)
    assert recovered_manifest["project_id"] == "orders-example"
    wrong_label = dict(env.labels, **{"io.faultline.project": "another-project"})
    container.attrs["Config"]["Labels"] = wrong_label
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        env.assert_owned(container)


def test_direct_engine_refuses_preflight_for_different_project(
    tmp_path, project_document, scenario
):
    project = Project.model_validate(project_document)
    with pytest.raises(ValueError, match="exact project and variant"):
        run_one(
            scenario,
            "correct",
            tmp_path,
            project=project,
            client=Mock(),
            platform_info={"images": {}},
        )
    assert not (tmp_path / "runs").exists()


def test_report_does_not_load_a_current_project(monkeypatch, tmp_path, scenario):
    bundle = Bundle(tmp_path, scenario, "correct")
    bundle.event("test", {})
    bundle.finalize(
        {
            "verdict": "PASS",
            "coverage": {"required_applied": 0, "required_total": 0},
            "cleanup": {"complete": True},
            "invariants": [],
            "diagnostics": [],
        }
    )
    monkeypatch.setattr(
        "faultline.cli.selected_project",
        Mock(side_effect=AssertionError("offline report must not load config")),
    )
    result = CliRunner().invoke(app, ["--project", "missing.yaml", "report", str(bundle.path)])
    assert result.exit_code == 0, result.output
    assert read_bundle(bundle.path)[1]["verdict"] == "PASS"
