"""Opt-in PostgreSQL/Docker acceptance; every trial provisions a fresh environment."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.adapters import OrdersAdapter, bundled_adapter
from faultline.artifacts import Bundle, read_bundle
from faultline.engine import run_one
from faultline.environment import Environment, client_and_preflight
from faultline.models import Scenario, digest, load_document, load_scenario
from faultline.projects import Project

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FAULTLINE_INTEGRATION") != "1",
        reason="set FAULTLINE_INTEGRATION=1 with local Docker images built",
    ),
]


@pytest.fixture(scope="module")
def docker_connection():
    client, info = client_and_preflight()
    yield client, info
    client.close()


@pytest.mark.parametrize(
    ("scenario_name", "variant", "expected", "debits"),
    [
        ("control", "unsafe", "PASS", 1),
        ("control", "correct", "PASS", 1),
        ("lost-response", "unsafe", "FAIL", 2),
        ("lost-response", "correct", "PASS", 1),
    ],
)
def test_fresh_environment_matrix(
    tmp_path, docker_connection, scenario_name, variant, expected, debits
):
    client, info = docker_connection
    scenario = load_scenario(ROOT / f"examples/orders/{scenario_name}.yaml")
    repeat = int(os.environ.get("FAULTLINE_ACCEPTANCE_REPEATS", "1"))
    assert 1 <= repeat <= 20
    for index in range(repeat):
        started = time.monotonic()
        path, report = run_one(scenario, variant, tmp_path, client=client, platform_info=info)
        assert report["verdict"] == expected, json.dumps(report["diagnostics"])
        assert report["cleanup"]["complete"]
        assert report["coverage"]["required_applied"] == report["coverage"]["required_total"]
        assert not report["diagnostics"]
        manifest, reread = read_bundle(path)
        assert reread["verdict"] == expected
        descriptor = json.loads((path / "adapter.json").read_text())
        assert descriptor["description"]["external_registration"] is False
        assert descriptor["description"]["invariants"] == dict.fromkeys(scenario.invariants, 1)
        assert report["adapter"] == manifest["adapter"]
        assert descriptor["identity"]["content_hash"] == report["adapter"]["content_hash"]
        snapshot = json.loads((path / "snapshots/final.json").read_text())
        assert len(snapshot["records"]["ledger"]) == debits
        assert snapshot["records"]["accounts"][0]["balance_minor"] == 1000000 - debits * 200000
        filters = {"label": f"io.faultline.run={report['run_id']}"}
        assert not client.containers.list(all=True, filters=filters)
        assert not client.networks.list(filters=filters)
        print(
            f"{scenario_name}/{variant} {index + 1}/{repeat}: {expected}, "
            f"{time.monotonic() - started:.2f}s, {path}",
            flush=True,
        )


def test_isolation_and_real_payload_conflict(tmp_path, scenario, docker_connection):
    client, info = docker_connection
    before_containers = {c.id for c in client.containers.list(all=True)}
    before_networks = {n.id for n in client.networks.list()}
    bundle = Bundle(tmp_path, scenario, "correct")
    env = Environment(client, bundle, info["images"])
    try:
        env.provision()
        bundled_adapter().prepare(env, "correct")
        env.start_application("correct")
        bundled_adapter().readiness(env)
        for container in env.containers.values():
            container.reload()
            assert not container.attrs["HostConfig"]["PortBindings"]
            assert not container.attrs["HostConfig"]["Privileged"]
            assert not container.attrs["HostConfig"]["Binds"]
        for network in env.networks.values():
            network.reload()
            assert network.attrs["Internal"]
        request = {
            "method": "POST",
            "path": "/orders",
            "timeout_ms": 2000,
            "headers": {"Idempotency-Key": "conflict-test", "Content-Type": "application/json"},
            "body": json.dumps(scenario.operations[0].request.body.model_dump()),
        }
        assert env.call("worker", "http", json.dumps(request), timeout=4)["status"] == 201
        payload = scenario.operations[0].request.body.model_dump()
        payload["quantity"] = 2
        request["body"] = json.dumps(payload)
        rejection = env.call("worker", "http", json.dumps(request), timeout=4)
        assert rejection["status"] == 409
        assert rejection["body"]["code"] == "idempotency_conflict"
        assert env.drain(10000)
        final = env.snapshot("final")
        assert final["quiescent"]
        assert len(final["records"]["ledger"]) == 1
        readonly = env.containers["observer"].exec_run(
            [
                "python",
                "-c",
                "from faultline.orders import connect; "
                "c=connect('observer'); c.execute('UPDATE accounts SET balance_minor=0')",
            ]
        )
        assert readonly.exit_code != 0
        assert b"read-only transaction" in readonly.output
    finally:
        assert env.cleanup()["complete"]
    assert before_containers <= {c.id for c in client.containers.list(all=True)}
    assert before_networks <= {n.id for n in client.networks.list()}


def test_malformed_adapter_finding_cannot_pass_real_purchase(
    monkeypatch, tmp_path, scenario, docker_connection
):
    client, info = docker_connection
    original_verify = OrdersAdapter.verify

    def forget_last_rule(self, *args, **kwargs):
        return original_verify(self, *args, **kwargs)[:-1]

    monkeypatch.setattr(OrdersAdapter, "verify", forget_last_rule)
    path, report = run_one(scenario, "correct", tmp_path, client=client, platform_info=info)
    assert report["verdict"] == "ERROR"
    assert report["coverage"]["required_applied"] == 1
    assert report["invariants"] == []
    assert any(
        d.get("hook") == "verify" and d["code"] == "adapter_contract_error"
        for d in report["diagnostics"]
    )
    final = json.loads((path / "snapshots/final.json").read_text())
    assert len(final["records"]["ledger"]) == 1
    assert final["records"]["accounts"][0]["balance_minor"] == 800000
    assert report["cleanup"]["complete"]
    assert read_bundle(path)[1]["verdict"] == "ERROR"
    filters = {"label": f"io.faultline.run={report['run_id']}"}
    assert not client.containers.list(all=True, filters=filters)
    assert not client.networks.list(filters=filters)


@pytest.mark.parametrize(
    ("variant", "verdict", "debits"), [("unsafe", "FAIL", 2), ("correct", "PASS", 1)]
)
def test_project_resources_images_and_verdicts(
    monkeypatch, tmp_path, scenario, docker_connection, variant, verdict, debits
):
    _client, base_info = docker_connection
    raw = load_document(ROOT / "examples/orders/faultline.project.yaml")
    raw["project_id"] = "project-integration"
    # A local immutable image ID is a supported application reference, not just the bundled tag.
    raw["variants"][variant]["application_image"] = base_info["images"]["runtime"]
    for service in raw["services"]:
        if service["id"] != "database":
            service["resources"] = {"memory_bytes": 201326592, "cpu_millis": 500, "pids_limit": 48}
    project = Project.model_validate(raw)
    inspected = []

    class InspectProject(Environment):
        def wait_application_ready(self):
            super().wait_application_ready()
            for service in project.services:
                container = self.containers[service.id]
                container.reload()
                host = container.attrs["HostConfig"]
                assert host["Memory"] == service.resources.memory_bytes
                assert host["NanoCpus"] == service.resources.cpu_millis * 1000000
                assert host["PidsLimit"] == service.resources.pids_limit
                assert (
                    container.attrs["Config"]["Labels"]["io.faultline.project"]
                    == project.project_id
                )
                assert host["ReadonlyRootfs"]
                assert not host["PortBindings"] and not host["Binds"]
                inspected.append(service.id)
            assert self.containers["application"].attrs["Image"] == self.images["application"]

    monkeypatch.setattr("faultline.engine.Environment", InspectProject)
    client, info = client_and_preflight(project=project, variant=variant)
    try:
        path, report = run_one(
            scenario, variant, tmp_path, client=client, platform_info=info, project=project
        )
        assert report["verdict"] == verdict, report["diagnostics"]
        assert set(inspected) == {"application", "database", "worker", "observer"}
        assert report["cleanup"]["complete"]
        assert report["coverage"]["required_applied"] == 1
        manifest, _ = read_bundle(path)
        config = json.loads((path / "project.json").read_text())["configuration"]
        assert config == project.configuration()
        assert report["project"]["configuration_hash"] == digest(config)
        assert manifest["project_id"] == project.project_id
        assert manifest["platform"]["images"]["application"] == info["images"]["application"]
        plan = json.loads((path / "plan.json").read_text())
        assert plan["scenario"]["limits"]["total_ms"] == 90000
        final = json.loads((path / "snapshots/final.json").read_text())
        assert len(final["records"]["ledger"]) == debits
        filters = {"label": f"io.faultline.run={report['run_id']}"}
        assert not client.containers.list(all=True, filters=filters)
        assert not client.networks.list(filters=filters)
    finally:
        client.close()


def test_cli_discovers_project_and_selects_its_only_variant(tmp_path, docker_connection):
    client, _info = docker_connection
    raw = load_document(ROOT / "examples/orders/faultline.project.yaml")
    del raw["variants"]["unsafe"]
    raw["project_id"] = "single-variant-example"
    for filename in raw["scenario_paths"]:
        (tmp_path / filename).write_bytes((ROOT / "examples/orders" / filename).read_bytes())
    (tmp_path / "faultline.project.yaml").write_text(json.dumps(raw))
    result = subprocess.run(
        [sys.executable, "-m", "faultline", "--format", "json", "run", "lost-response.yaml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    envelope = json.loads(result.stdout)
    path = Path(envelope["artifact_paths"][0])
    manifest, report = read_bundle(path)
    assert manifest["variant"] == "correct"
    assert manifest["project_id"] == "single-variant-example"
    assert report["cleanup"]["complete"]
    filters = {"label": f"io.faultline.run={report['run_id']}"}
    assert not client.containers.list(all=True, filters=filters)
    assert not client.networks.list(filters=filters)


def test_final_client_timeout_can_still_be_business_success(tmp_path, scenario, docker_connection):
    client, info = docker_connection
    raw = scenario.model_dump(by_alias=True)
    raw["operations"][0]["retry"] = {"max_attempts": 1, "on": [], "backoff_ms": []}
    one_attempt = Scenario.model_validate(raw)
    _path, result = run_one(one_attempt, "correct", tmp_path, client=client, platform_info=info)
    assert result["attempts"][0]["outcome"] == "timeout"
    assert result["coverage"]["required_applied"] == 1
    assert result["verdict"] == "PASS"
    assert result["cleanup"]["complete"]


def test_unmatched_required_fault_is_inconclusive(tmp_path, scenario, docker_connection):
    client, info = docker_connection
    raw = scenario.model_dump(by_alias=True)
    raw["faults"][0]["trigger"]["status"] = 202
    unmatched = Scenario.model_validate(raw)
    _path, result = run_one(unmatched, "correct", tmp_path, client=client, platform_info=info)
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["coverage"]["required_applied"] == 0
    assert all(check["status"] == "PASS" for check in result["invariants"])
    assert result["cleanup"]["complete"]


def test_real_sigterm_cleans_only_the_child_run(tmp_path, docker_connection):
    client, info = docker_connection
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "faultline",
            "--output",
            str(tmp_path),
            "--format",
            "json",
            "run",
            str(ROOT / "examples/orders/lost-response.yaml"),
            "--variant",
            "correct",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    manifests = []
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            manifests = list((tmp_path / "runs").glob("*/manifest.json"))
            if manifests:
                manifest = json.loads(manifests[0].read_text())
                if manifest["stage"] in {"ready", "executing"}:
                    child.send_signal(signal.SIGTERM)
                    break
            assert child.poll() is None, "CLI exited before interruption test reached execution"
            time.sleep(0.05)
        else:
            pytest.fail("child did not become ready before deadline")
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 130, (stdout, stderr)
        result = json.loads(stdout)
        assert result["exit_code"] == 130
        bundle = Path(result["artifact_paths"][0])
        _manifest, report = read_bundle(bundle)
        assert report["lifecycle"] == "interrupted"
        assert report["verdict"] == "INCONCLUSIVE"
        assert report["cleanup"]["complete"]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        # Preserve evidence, even on a failed assertion; only exact recorded ownership is eligible.
        for path in manifests:
            manifest = json.loads(path.read_text())
            env = Environment(
                client,
                SimpleNamespace(manifest=manifest, run_id=manifest["run_id"]),
                info["images"],
            )
            assert env.cleanup()["complete"]


def test_sigkill_recovery_preserves_other_runs_and_original_evidence(
    tmp_path, scenario, docker_connection
):
    client, info = docker_connection
    output = tmp_path / "interrupted"
    # A second real environment outside the selected root must survive stale cleanup.
    protected_bundle = Bundle(tmp_path / "protected", scenario, "correct")
    protected = Environment(client, protected_bundle, info["images"])
    raw = scenario.model_dump(by_alias=True)
    raw["operations"][0]["timeout_ms"] = 10000
    scenario_path = tmp_path / "slow-response.json"
    scenario_path.write_text(json.dumps(raw))
    child = None
    manifest_path = None
    command = [sys.executable, "-m", "faultline", "--output", str(output), "--format", "json"]
    try:
        protected.provision()
        bundled_adapter().prepare(protected, "correct")
        child = subprocess.Popen(
            [*command, "run", str(scenario_path), "--variant", "correct"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            paths = list((output / "runs").glob("*/manifest.json"))
            if paths:
                manifest_path = paths[0]
                history = manifest_path.parent / "history.ndjson"
                if history.exists() and '"type":"fault_applied"' in history.read_text():
                    break
            assert child.poll() is None, "child exited before controlled interruption"
            time.sleep(0.05)
        else:
            pytest.fail("child never reached response suppression")

        live_cleanup = subprocess.run(
            [*command, "clean", "--stale"], capture_output=True, text=True, timeout=10
        )
        assert live_cleanup.returncode == 2, (live_cleanup.stdout, live_cleanup.stderr)
        assert "Another Faultline" in live_cleanup.stdout
        assert child.poll() is None
        child.kill()  # SIGKILL cannot run finally blocks or ordinary cleanup handlers.
        child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL
        manifest = json.loads(manifest_path.read_text())
        filters = {"label": f"io.faultline.run={manifest['run_id']}"}
        assert client.containers.list(all=True, filters=filters), "expected real leftovers"
        original = {
            path.relative_to(manifest_path.parent): path.read_bytes()
            for path in manifest_path.parent.rglob("*")
            if path.is_file()
        }
        assert "report.json" not in {str(path) for path in original}
        for _ in range(2):  # Repeating scoped cleanup is safe after resources have disappeared.
            cleanup = subprocess.run(
                [*command, "clean", "--stale"], capture_output=True, text=True, timeout=30
            )
            assert cleanup.returncode == 0, (cleanup.stdout, cleanup.stderr)
            result = json.loads(cleanup.stdout)
            assert result["summary"]["complete"]
            assert result["run_ids"] == [manifest["run_id"]]
            receipt = json.loads(Path(result["artifact_paths"][0]).read_text())
            assert receipt["complete"]
            assert receipt["recorded_resources"] == manifest["resources"]
        assert len(list((output / "cleanup").glob("*.json"))) == 2
        assert not client.containers.list(all=True, filters=filters)
        assert not client.networks.list(filters=filters)
        assert original == {
            path.relative_to(manifest_path.parent): path.read_bytes()
            for path in manifest_path.parent.rglob("*")
            if path.is_file()
        }
        incomplete = subprocess.run(
            [*command, "report", manifest["run_id"]], capture_output=True, text=True, timeout=10
        )
        assert incomplete.returncode == 4  # Cleaning resources cannot invent a business verdict.
        for resource in [*protected.containers.values(), *protected.networks.values()]:
            protected.assert_owned(resource)
        assert protected.snapshot("baseline")["complete"]
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        if manifest_path is not None:
            manifest = json.loads(manifest_path.read_text())
            env = Environment(
                client, SimpleNamespace(manifest=manifest, run_id=manifest["run_id"]), {}
            )
            assert env.cleanup()["complete"]
        assert protected.cleanup()["complete"]


def test_observation_deadline_interrupts_real_helper_and_cleans(
    monkeypatch, tmp_path, scenario, docker_connection
):
    client, info = docker_connection

    class SlowObserver(Environment):
        def snapshot(self, role):
            if role == "final":
                # A real exec stream waiting for a real child, not a mocked timeout return.
                self.containers["observer"].exec_run(
                    ["python", "-c", "import time; time.sleep(30)"]
                )
            return super().snapshot(role)

    monkeypatch.setattr("faultline.engine.Environment", SlowObserver)
    raw = scenario.model_dump(by_alias=True)
    raw["limits"]["observation_ms"] = 1000
    started = time.monotonic()
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert time.monotonic() - started < 25  # The 30-second helper was actually interrupted.
    assert report["verdict"] == "ERROR"
    assert report["cleanup"]["complete"]
    assert any(d.get("stage") == "observing" for d in report["diagnostics"])
    assert not (path / "snapshots/final.json").exists()
    assert read_bundle(path)[1]["verdict"] == "ERROR"
    filters = {"label": f"io.faultline.run={report['run_id']}"}
    assert not client.containers.list(all=True, filters=filters)
    assert not client.networks.list(filters=filters)


def test_drain_deadline_prevents_pass_with_a_still_running_application(
    monkeypatch, tmp_path, scenario, docker_connection
):
    client, info = docker_connection

    class SlowDrain(Environment):
        def drain(self, timeout_ms):
            time.sleep(30)
            return super().drain(timeout_ms)

    monkeypatch.setattr("faultline.engine.Environment", SlowDrain)
    raw = scenario.model_dump(by_alias=True)
    raw["limits"]["drain_ms"] = 1000
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert report["verdict"] == "INCONCLUSIVE"
    assert any(d.get("stage") == "draining" for d in report["diagnostics"])
    assert any(f["status"] == "UNKNOWN" for f in report["invariants"])
    final = json.loads((path / "snapshots/final.json").read_text())
    assert len(final["records"]["ledger"]) == 1
    assert not final["quiescent"]
    assert report["cleanup"]["complete"]
    filters = {"label": f"io.faultline.run={report['run_id']}"}
    assert not client.containers.list(all=True, filters=filters)
    assert not client.networks.list(filters=filters)


@pytest.mark.parametrize("use_project", [False, True])
def test_cleanup_deadline_preserves_real_leftovers_for_later_recovery(
    monkeypatch, tmp_path, scenario, docker_connection, use_project
):
    client, info = docker_connection
    project = None
    if use_project:
        project = Project.model_validate(
            load_document(ROOT / "examples/orders/faultline.project.yaml")
        )
        client, info = client_and_preflight(project=project, variant="correct")

    class StalledCleanup(Environment):
        def cleanup(self):
            self.cleanup_remaining = list(self.bundle.manifest["resources"])
            time.sleep(30)  # Simulate a stuck cleanup stage while real owned resources exist.
            return super().cleanup()

    monkeypatch.setattr("faultline.engine.Environment", StalledCleanup)
    # Preserve omitted defaults; serializing all defaults would explicitly request
    # total_ms=300000 and correctly exceed the project's 90000-ms ceiling.
    raw = scenario.model_dump(by_alias=True, exclude_unset=True)
    raw.setdefault("limits", {})["cleanup_ms"] = 2000
    try:
        path, report = run_one(
            Scenario.model_validate(raw),
            "correct",
            tmp_path,
            client=client,
            platform_info=info,
            project=project,
        )
        assert report["verdict"] == "ERROR"
        assert all(f["status"] == "PASS" for f in report["invariants"])
        assert not report["cleanup"]["complete"]
        assert len(report["cleanup"]["remaining_resources"]) == 6
        assert any(d.get("stage") == "cleanup" for d in report["diagnostics"])
        filters = {"label": f"io.faultline.run={report['run_id']}"}
        assert client.containers.list(all=True, filters=filters)
        original_report = (path / "report.json").read_bytes()
        recovered = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "--output",
                str(tmp_path),
                "--format",
                "json",
                "clean",
                "--run",
                report["run_id"],
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
        assert json.loads(recovered.stdout)["summary"]["complete"]
        assert not client.containers.list(all=True, filters=filters)
        assert not client.networks.list(filters=filters)
        assert (path / "report.json").read_bytes() == original_report
        assert read_bundle(path)[1]["verdict"] == "ERROR"
    finally:
        for manifest_path in (tmp_path / "runs").glob("*/manifest.json"):
            manifest = json.loads(manifest_path.read_text())
            env = Environment(
                client, SimpleNamespace(manifest=manifest, run_id=manifest["run_id"]), {}
            )
            assert env.cleanup()["complete"]
        if use_project:
            client.close()
