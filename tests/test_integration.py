"""Opt-in PostgreSQL/Docker acceptance; every trial provisions a fresh environment."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from copy import deepcopy
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


@pytest.mark.parametrize("duplicate", [False, True])
def test_concurrent_orders_reconcile_shared_authority(tmp_path, docker_connection, duplicate):
    client, info = docker_connection
    raw = load_document(ROOT / "examples/orders/control.yaml")
    first = raw["operations"][0]
    raw["concurrency"] = 3
    raw["barriers"] = [{"id": "together", "members": [f"op_{i}" for i in range(3)]}]
    raw["operations"] = []
    for index in range(3):
        op = deepcopy(first)
        op.update(id=f"op_{index}", barrier="together")
        if not duplicate:
            op["logical_id"] = op["request"]["json"]["purchase_id"] = f"purchase-{index}"
            op["request"]["headers"]["Idempotency-Key"] = f"key-{index}"
        raw["operations"].append(op)
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert report["verdict"] == "PASS", report["diagnostics"]
    final = json.loads((path / "snapshots/final.json").read_text())["records"]
    count = 1 if duplicate else 3
    assert len(final["orders"]) == len(final["ledger"]) == count
    assert final["accounts"][0]["balance_minor"] == 1000000 - 200000 * count
    assert len(report["invariants"]) == 7 * count
    history = [json.loads(line) for line in (path / "history.ndjson").read_text().splitlines()]
    assert report["cleanup"]["complete"]
    assert read_bundle(path)[1]["verdict"] == "PASS"
    print(f"concurrent duplicate={duplicate}: {path}, events={len(history)}", flush=True)


@pytest.mark.parametrize("kind", ["proxy_latency", "proxy_disconnect", "process_restart"])
def test_acknowledged_fault_matrix(tmp_path, docker_connection, kind):
    client, info = docker_connection
    raw = load_document(ROOT / "examples/orders/control.yaml")
    raw["operations"][0].update(
        after_faults=["impair"],
        timeout_ms=1000,
        retry={
            "max_attempts": 3,
            "on": ["timeout", "connection_error", "http_503"],
            "backoff_ms": [1000, 1000],
        },
    )
    fault = {"id": "impair", "kind": kind, "target": "database", "trigger": {"offset_ms": 0}}
    if kind == "proxy_latency":
        fault.update(latency_ms=200, direction="downstream", duration_ms=600)
    elif kind == "proxy_disconnect":
        fault.update(duration_ms=500)
    else:
        fault.update(target="application", downtime_ms=500)
    raw["faults"] = [fault]
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert report["verdict"] == "PASS", report
    assert report["coverage"]["required_applied"] == 1
    assert report["cleanup"]["complete"]
    history = [json.loads(line) for line in (path / "history.ndjson").read_text().splitlines()]
    print(
        f"fault {kind}: {path}, outcomes={[a['outcome'] for a in report['attempts']]}", flush=True
    )
    assert len(history) > 10


@pytest.mark.parametrize(
    "expectation", ["insufficient_funds", "invalid_quantity", "idempotency_conflict"]
)
def test_expected_rejections_are_verified_without_new_effects(
    tmp_path, docker_connection, expectation
):
    client, info = docker_connection
    raw = load_document(ROOT / "examples/orders/control.yaml")
    op = raw["operations"][0]
    if expectation == "idempotency_conflict":
        rejected = deepcopy(op)
        rejected.update(id="conflict", after=[op["id"]], expectation=expectation)
        rejected["request"]["json"]["quantity"] = 2
        raw["operations"].append(rejected)
    else:
        op["expectation"] = expectation
        op["request"]["json"]["quantity"] = 6 if expectation == "insufficient_funds" else 0
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert report["verdict"] == "PASS", report
    state = json.loads((path / "snapshots/final.json").read_text())["records"]
    assert len(state["ledger"]) == (1 if expectation == "idempotency_conflict" else 0)


def test_compatible_faults_compose_with_acknowledged_gate(tmp_path, docker_connection):
    client, info = docker_connection
    raw = load_document(ROOT / "examples/orders/lost-response.yaml")
    raw["operations"][0]["after_faults"] = ["latency"]
    raw["operations"][0]["timeout_ms"] = 2000
    raw["faults"].append(
        {
            "id": "latency",
            "kind": "proxy_latency",
            "target": "database",
            "trigger": {"offset_ms": 0},
            "latency_ms": 20,
            "direction": "downstream",
            "duration_ms": 400,
        }
    )
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert report["verdict"] == "PASS", report
    assert report["coverage"]["required_applied"] == 2 and report["cleanup"]["complete"]
    assert read_bundle(path)[1]["verdict"] == "PASS"


@pytest.mark.parametrize("failure", ["worker_stream", "disk_full"])
def test_execution_harness_failures_retain_bundle_and_cleanup(
    tmp_path, docker_connection, monkeypatch, failure
):
    client, info = docker_connection
    scenario = load_scenario(ROOT / "examples/orders/control.yaml")
    if failure == "worker_stream":

        class LostWorker(Environment):
            def wait_application_ready(self):
                super().wait_application_ready()
                self.worker_session.close()

        monkeypatch.setattr("faultline.engine.Environment", LostWorker)
    else:
        original = Bundle.event

        def disk_full(self, kind, payload, **refs):
            if kind == "attempt_dispatched":
                raise OSError(28, "No space left on device (injected at artifact boundary)")
            return original(self, kind, payload, **refs)

        monkeypatch.setattr(Bundle, "event", disk_full)
    path, report = run_one(scenario, "correct", tmp_path, client=client, platform_info=info)
    assert report["verdict"] == "ERROR" and report["cleanup"]["complete"]
    assert (path / "plan.json").is_file() and (path / "snapshots/baseline.json").is_file()
    assert read_bundle(path)[1]["verdict"] == "ERROR"
    filters = {"label": f"io.faultline.run={report['run_id']}"}
    assert not client.containers.list(all=True, filters=filters)
    assert not client.networks.list(filters=filters)


@pytest.mark.parametrize("crash", [False, True])
def test_handler_outlives_timeout_and_registry_reconciles(
    tmp_path, docker_connection, monkeypatch, crash
):
    client, info = docker_connection
    raw = load_document(ROOT / "examples/orders/control.yaml")
    raw["operations"][0].update(timeout_ms=300)
    raw["operations"][0]["retry"] = {"max_attempts": 1, "on": [], "backoff_ms": []}
    if crash:
        raw["faults"] = [
            {
                "id": "crash_handler",
                "kind": "process_restart",
                "target": "application",
                "trigger": {
                    "event": "client_outcome",
                    "operation_id": raw["operations"][0]["id"],
                    "attempt": 1,
                },
                "downtime_ms": 0,
            }
        ]

    class DelayedEnvironment(Environment):
        def create_container(self, role, networks, **kwargs):
            if role == "application":
                kwargs["environment"]["FL_TEST_HANDLER_DELAY_MS"] = "3000" if crash else "1000"
            return super().create_container(role, networks, **kwargs)

    monkeypatch.setattr("faultline.engine.Environment", DelayedEnvironment)
    path, report = run_one(
        Scenario.model_validate(raw), "correct", tmp_path, client=client, platform_info=info
    )
    assert report["verdict"] == ("FAIL" if crash else "PASS"), report
    assert report["attempts"][0]["outcome"] == "timeout"
    final = json.loads((path / "snapshots/final.json").read_text())
    assert final["quiescent"] and final["application_sessions"] == 0
    assert len(final["records"]["orders"]) == (0 if crash else 1)
    history = [json.loads(line) for line in (path / "history.ndjson").read_text().splitlines()]
    assert any(e["type"] == "inflight_stale_cleared" for e in history)
    print(f"delayed handler crash={crash}: {path}", flush=True)


@pytest.fixture(scope="module")
def docker_connection():
    client, info = client_and_preflight()
    yield client, info
    client.close()


@pytest.fixture(scope="module")
def installed_adapter_site(tmp_path_factory):
    """Build a separate wheel and install it without altering Faultline's environment."""
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv, "Run integration through uv run (or put uv on PATH) to build the example wheel"
    root = tmp_path_factory.mktemp("packaged-adapter")
    built = subprocess.run(
        [uv, "build", "--wheel", str(ROOT / "examples/installed-orders-adapter"), "-o", str(root)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert built.returncode == 0, (built.stdout, built.stderr)
    wheels = list(root.glob("*.whl"))
    assert len(wheels) == 1
    site = root / "site"
    installed = subprocess.run(
        [uv, "pip", "install", "--no-deps", "--offline", "--target", str(site), str(wheels[0])],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    return site


@pytest.fixture(scope="module")
def counter_adapter_site(tmp_path_factory):
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv
    root = tmp_path_factory.mktemp("counter-package")
    source = ROOT / "examples/counter-adapter"
    subprocess.run(
        [uv, "build", "--wheel", str(source), "-o", str(root)],
        check=True,
        capture_output=True,
        timeout=90,
    )
    (wheel,) = root.glob("*.whl")
    site = root / "site"
    subprocess.run(
        [uv, "pip", "install", "--no-deps", "--offline", "--target", str(site), str(wheel)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["docker", "build", "-t", "faultline-counter:0.1.0", str(source)],
        check=True,
        capture_output=True,
        timeout=90,
    )
    return site


@pytest.mark.parametrize(
    "scenario_name,variant,expected,total",
    [
        ("lost-response.yaml", "unsafe", "FAIL", 14),
        ("lost-response.yaml", "correct", "PASS", 7),
        ("generated.yaml", "correct", "PASS", None),
    ],
)
def test_independent_counter_adapter_without_core_changes(
    tmp_path, counter_adapter_site, scenario_name, variant, expected, total
):
    command = [
        sys.executable,
        "-m",
        "faultline",
        "--output",
        str(tmp_path),
        "--format",
        "json",
        "--project",
        str(ROOT / "examples/counter-adapter/faultline.project.yaml"),
    ]
    result = subprocess.run(
        [*command, "run", scenario_name, "--variant", variant],
        env=dict(os.environ, PYTHONPATH=str(counter_adapter_site)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == (1 if expected == "FAIL" else 0), (result.stdout, result.stderr)
    path = Path(json.loads(result.stdout)["artifact_paths"][0])
    manifest, report = read_bundle(path)
    assert manifest["adapter"]["adapter_id"] == "counter_example"
    assert report["verdict"] == expected and report["cleanup"]["complete"]
    snapshot = json.loads((path / "snapshots/final.json").read_text())
    if total is None:
        plan = json.loads((path / "plan.json").read_text())
        operations = plan["scenario"]["operations"]
        assert len(operations) == 3
        assert plan["generators"][0]["prng"] == "counter-sha256-v1"
        assert plan["generators"][0]["version"] == 1
        assert plan["scenario"]["generators"] == []
        total = sum(op["request"]["json"]["delta"] for op in operations)
        assert len(snapshot["records"]["increments"]) == 3
    assert snapshot["records"]["counter"] == [{"total": total}]
    assert set(snapshot["records"]) == {"counter", "increments"}
    # Offline reporting cannot import this separately installed adapter.
    result = subprocess.run(
        [sys.executable, "-m", "faultline", "report", str(path)],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (1 if expected == "FAIL" else 0)
    print(f"external counter/{variant}: {expected}, total={total}, {path}", flush=True)


def test_builtin_generated_workload_has_materialized_authoritative_effects(
    tmp_path, docker_connection
):
    client, info = docker_connection
    scenario = load_scenario(ROOT / "examples/orders/generated.yaml")
    path, report = run_one(scenario, "correct", tmp_path, client=client, platform_info=info)
    assert report["verdict"] == "PASS" and report["cleanup"]["complete"]
    plan = json.loads((path / "plan.json").read_text())
    final = json.loads((path / "snapshots/final.json").read_text())
    operations = plan["scenario"]["operations"]
    assert len(final["records"]["orders"]) == len(operations) == 3
    assert plan["generators"][0]["prng"] == "sha256-counter-v1"
    assert sum(row["amount_minor"] for row in final["records"]["ledger"]) == 600000


@pytest.mark.parametrize(
    ("entry_point", "variant", "expected", "debits"),
    [
        ("example_orders", "unsafe", "FAIL", 2),
        ("example_orders", "correct", "PASS", 1),
        ("incomplete_orders", "correct", "ERROR", 1),
    ],
)
def test_installed_wheel_runs_and_offline_tools_survive_its_absence(
    tmp_path, docker_connection, installed_adapter_site, entry_point, variant, expected, debits
):
    client, _info = docker_connection
    raw = load_document(ROOT / "examples/orders/faultline.project.yaml")
    raw["adapter"]["entry_point"] = entry_point
    raw["scenario_paths"] = ["lost-response.yaml"]
    project = tmp_path / "faultline.project.yaml"
    project.write_text(json.dumps(raw))
    shutil.copyfile(ROOT / "examples/orders/lost-response.yaml", tmp_path / "lost-response.yaml")
    output = tmp_path / "output"
    command = [sys.executable, "-m", "faultline", "--output", str(output), "--format", "json"]
    environment = dict(os.environ, PYTHONPATH=str(installed_adapter_site))
    result = subprocess.run(
        [*command, "--project", str(project), "run", "lost-response.yaml", "--variant", variant],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    assert result.returncode == {"PASS": 0, "FAIL": 1, "ERROR": 4}[expected], (
        result.stdout,
        result.stderr,
    )
    envelope = json.loads(result.stdout)
    assert envelope["summary"]["verdict"] == expected
    path = Path(envelope["artifact_paths"][0])
    manifest, report = read_bundle(path)
    assert report["cleanup"]["complete"]
    assert report["coverage"]["required_applied"] == 1
    assert manifest["adapter"]["adapter_id"] == entry_point
    assert manifest["adapter"]["package"] == "faultline-example-orders"
    identity = json.loads((path / "adapter.json").read_text())["identity"]
    assert identity["entry_point"] == entry_point
    assert identity["content_hash"] == report["adapter"]["content_hash"]
    assert "faultline_example_orders/__init__.py" in identity["files"]
    final = json.loads((path / "snapshots/final.json").read_text())
    assert len(final["records"]["ledger"]) == debits
    assert final["records"]["accounts"][0]["balance_minor"] == 1000000 - 200000 * debits
    if expected == "ERROR":
        assert not report["invariants"]
        assert any(
            d.get("code") == "adapter_contract_error" and d.get("hook") == "verify"
            for d in report["diagnostics"]
        )
    else:
        assert not report["diagnostics"]
    # No plugin PYTHONPATH and no project file: offline inspection and recovery must
    # still work, even for the malformed-verifier run. Evidence remains byte-for-byte.
    project.unlink()
    original = (path / "report.json").read_bytes()
    offline_environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    offline = subprocess.run(
        [*command, "report", report["run_id"]],
        env=offline_environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert offline.returncode == result.returncode, (offline.stdout, offline.stderr)
    cleaned = subprocess.run(
        [*command, "clean", "--run", report["run_id"]],
        env=offline_environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert cleaned.returncode == 0, (cleaned.stdout, cleaned.stderr)
    assert (path / "report.json").read_bytes() == original
    filters = {"label": f"io.faultline.run={report['run_id']}"}
    assert not client.containers.list(all=True, filters=filters)
    assert not client.networks.list(filters=filters)
    print(f"installed {entry_point}/{variant}: {expected}, debits={debits}, {path}", flush=True)


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
        app_networks = env.containers["application"].attrs["NetworkSettings"]["Networks"]
        assert set(app_networks) == {env.networks[name].name for name in ("client", "dependency")}
        proxy_networks = env.containers["proxy"].attrs["NetworkSettings"]["Networks"]
        admin_ip = proxy_networks[env.networks["control"].name]["IPAddress"]
        data_ip = proxy_networks[env.networks["dependency"].name]["IPAddress"]
        database_ip = env.containers["database"].attrs["NetworkSettings"]["Networks"][
            env.networks["storage"].name
        ]["IPAddress"]
        probe = """import socket,sys,json
for host,port in json.loads(sys.argv[1]):
    try:
        connection=socket.create_connection((host,port),timeout=0.3)
    except OSError:
        continue
    connection.close()
    raise AssertionError('forbidden direct connection succeeded')
"""
        blocked = env.containers["application"].exec_run(
            [
                "python",
                "-c",
                probe,
                json.dumps(
                    [[admin_ip, 8474], [data_ip, 8474], [database_ip, 5432], ["database", 5432]]
                ),
            ]
        )
        assert blocked.exit_code == 0, blocked.output
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


def test_proxy_disable_breaks_existing_and_new_database_connections(
    tmp_path, scenario, docker_connection
):
    client, info = docker_connection
    bundle = Bundle(tmp_path, scenario, "correct")
    env = Environment(client, bundle, info["images"])
    results = []
    try:
        env.provision()
        bundled_adapter().prepare(env, "correct")
        env.start_application("correct")
        env.wait_application_ready()
        app = env.containers["application"]
        script = (
            "from faultline.orders import connect; c=connect(); c.execute('SELECT pg_sleep(4)')"
        )
        thread = threading.Thread(
            target=lambda: results.append(app.exec_run(["python", "-c", script])), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = env.call("observer", "registry", "", timeout=5)
            if state["application_sessions"]:
                break
            time.sleep(0.05)
        assert state["application_sessions"] > 0
        disabled = env.call("worker", "proxy", json.dumps({"action": "disable"}), timeout=5)
        assert disabled["acknowledged"] and disabled["configuration"]["enabled"] is False
        thread.join(timeout=3)
        assert not thread.is_alive() and results[0].exit_code != 0
        fresh = app.exec_run(
            ["python", "-c", "from faultline.orders import connect; connect().execute('SELECT 1')"]
        )
        assert fresh.exit_code != 0
        enabled = env.call("worker", "proxy", json.dumps({"action": "enable"}), timeout=5)
        assert enabled["acknowledged"] and enabled["configuration"]["enabled"] is True
        healed = app.exec_run(
            ["python", "-c", "from faultline.orders import connect; connect().execute('SELECT 1')"]
        )
        assert healed.exit_code == 0, healed.output
    finally:
        assert env.cleanup()["complete"]


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
        assert set(inspected) == {"application", "database", "worker", "observer", "proxy"}
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


@pytest.mark.parametrize(("kind", "role"), [("network", "client"), ("container", "database")])
def test_sigkill_between_docker_creation_and_id_recording(tmp_path, docker_connection, kind, role):
    client, _info = docker_connection
    output = tmp_path / "interrupted-create"
    marker = tmp_path / "created.json"
    # This marker belongs only to the test: recovery cannot read the returned ID from it.
    script = """
import json, sys, time
from pathlib import Path
from faultline.environment import Environment
from faultline.engine import run_one
from faultline.models import load_scenario
from faultline.artifacts import atomic_json
output, marker, wanted_kind, wanted_role, scenario = sys.argv[1:]
original = Environment.record
def pause(self, kind, resource, role):
    if (kind, role) == (wanted_kind, wanted_role):
        atomic_json(Path(marker), {'id': resource.id, 'run_id': self.bundle.run_id})
        time.sleep(60)
    original(self, kind, resource, role)
Environment.record = pause
run_one(load_scenario(Path(scenario)), 'correct', Path(output))
"""
    command = [sys.executable, "-m", "faultline", "--output", str(output), "--format", "json"]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(output),
            str(marker),
            kind,
            role,
            str(ROOT / "examples/orders/control.yaml"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        until = time.monotonic() + 40
        while not marker.exists():
            assert child.poll() is None, child.communicate(timeout=1)
            assert time.monotonic() < until, "create boundary was not reached"
            time.sleep(0.05)
        created = json.loads(marker.read_text())
        manifest_path = output / "runs" / created["run_id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert created["id"] not in {r["id"] for r in manifest["resources"]}
        intent = next(i for i in manifest["creation_intents"] if i["role"] == role)
        collection = client.containers if kind == "container" else client.networks
        assert collection.get(intent["name"]).id == created["id"]
        busy = subprocess.run(
            [*command, "clean", "--run", created["run_id"]],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert busy.returncode == 2
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL
        original = {
            p.relative_to(manifest_path.parent): p.read_bytes()
            for p in manifest_path.parent.rglob("*")
            if p.is_file()
        }
        for _ in range(2):
            cleaned = subprocess.run(
                [*command, "clean", "--run", created["run_id"]],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert cleaned.returncode == 0, (cleaned.stdout, cleaned.stderr)
            receipt = json.loads(Path(json.loads(cleaned.stdout)["artifact_paths"][0]).read_text())
            assert receipt["reconciled_resources"] == [
                {"kind": kind, "role": role, "id": created["id"]}
            ]
        binding = json.loads(
            (output / "cleanup/bindings" / f"{created['run_id']}.json").read_text()
        )
        assert binding["resources"][0]["id"] == created["id"]
        assert original == {
            p.relative_to(manifest_path.parent): p.read_bytes()
            for p in manifest_path.parent.rglob("*")
            if p.is_file()
        }
        filters = {"label": f"io.faultline.run={created['run_id']}"}
        assert not client.containers.list(all=True, filters=filters)
        assert not client.networks.list(filters=filters)
        print(f"creation gap {kind}/{role}: recovered {created['id']}, run={created['run_id']}")
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        if (output / "runs").exists():
            cleanup = subprocess.run(
                [*command, "clean", "--stale"], capture_output=True, text=True, timeout=30
            )
            assert cleanup.returncode == 0, (cleanup.stdout, cleanup.stderr)


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
        assert len(report["cleanup"]["remaining_resources"]) == 9
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
