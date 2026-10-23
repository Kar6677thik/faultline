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

from faultline.artifacts import Bundle, read_bundle
from faultline.engine import run_one
from faultline.environment import Environment, client_and_preflight
from faultline.models import Scenario, load_scenario

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
        assert read_bundle(path)[1]["verdict"] == expected
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
        env.provision("correct")
        env.start_application("correct")
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
