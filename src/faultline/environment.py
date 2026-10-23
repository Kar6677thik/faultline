"""Only locally created, manifest-recorded resources can be mutated or deleted."""

import json
import os
import platform
import queue
import secrets
import threading
import time

import docker
from docker.errors import NotFound

from faultline.worker import WorkerSession

POSTGRES_IMAGE = "postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb"
RUNTIME_IMAGE = "faultline-runtime:0.1.0"
LABEL_PREFIX = "io.faultline"


class PreflightError(ValueError):
    pass


def client_and_preflight(*, require_images=True):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise PreflightError(
            "Execution requires Linux x86-64; use Debian/Ubuntu in WSL2 on Windows"
        )
    host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if host != "unix:///var/run/docker.sock":
        raise PreflightError("Only the local /var/run/docker.sock daemon is supported")
    client = docker.DockerClient(base_url=host, timeout=15)
    info = client.info()
    if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
        client.close()
        raise PreflightError("Docker must run Linux x86-64 containers")
    try:
        images = (
            {
                "runtime": client.images.get(RUNTIME_IMAGE).id,
                "postgres": client.images.get(POSTGRES_IMAGE).id,
            }
            if require_images
            else {}
        )
    except NotFound as exc:
        client.close()
        raise PreflightError("Images missing; follow README build/pull commands first") from exc
    return client, {
        "os": platform.system(),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "docker": client.version()["Version"],
        "images": images,
    }


class Environment:
    def __init__(self, client, bundle, images: dict):
        self.client, self.bundle, self.images = client, bundle, images
        self.labels = {
            f"{LABEL_PREFIX}.managed": "true",
            f"{LABEL_PREFIX}.project": "orders-m1",
            f"{LABEL_PREFIX}.run": bundle.run_id,
            f"{LABEL_PREFIX}.nonce": bundle.manifest["ownership_nonce"],
        }
        self.containers = {}
        self.networks = {}
        self.worker_session = None
        self.passwords = {
            f"FL_{role.upper()}_PASSWORD": secrets.token_urlsafe(32)
            for role in ("setup", "app", "observer")
        }

    def record(self, kind, resource, role):
        self.bundle.manifest["resources"].append({"kind": kind, "id": resource.id, "role": role})
        self.bundle.save_manifest()

    def assert_owned(self, resource):
        resource.reload()
        labels = resource.attrs.get("Labels") or resource.attrs.get("Config", {}).get("Labels", {})
        if any(labels.get(key) != value for key, value in self.labels.items()):
            raise RuntimeError("resource ownership mismatch; refusing mutation")
        if resource.id not in {r["id"] for r in self.bundle.manifest["resources"]}:
            raise RuntimeError("resource missing from ownership manifest")

    def create_container(self, role, networks, *, command=None, environment=None, database=False):
        tmpfs = {"/tmp": "rw,noexec,nosuid,size=16777216"}
        if database:
            tmpfs.update(
                {
                    "/var/lib/postgresql/data": "rw,nosuid,size=134217728,uid=70,gid=70,mode=0700",
                    "/var/run/postgresql": "rw,nosuid,size=16777216,uid=70,gid=70",
                }
            )
        container = self.client.containers.create(
            self.images["postgres" if database else "runtime"],
            command=command,
            name=f"fl-{self.bundle.run_id}-{role}",
            hostname=role,
            environment=environment or {},
            labels=self.labels,
            network=self.networks[networks[0]].id,
            networking_config={
                self.networks[networks[0]].id: self.client.api.create_endpoint_config(
                    aliases=[role]
                )
            },
            user="postgres" if database else "10001:10001",
            read_only=True,
            tmpfs=tmpfs,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="256m",
            nano_cpus=1000000000,
            pids_limit=64,
            restart_policy={"Name": "no"},
            detach=True,
            log_config=docker.types.LogConfig(
                type="local", config={"max-size": "1m", "max-file": "1", "compress": "false"}
            ),
        )
        self.record("container", container, role)
        self.containers[role] = container
        # Attach aliases before startup. Docker also resolves the service hostname.
        for network in networks[1:]:
            self.networks[network].connect(container, aliases=[role])
        self.assert_owned(container)
        container.start()
        return container

    def provision(self, variant):
        for role in ("client", "storage"):
            network = self.client.networks.create(
                f"fl-{self.bundle.run_id}-{role}",
                internal=True,
                labels=self.labels,
            )
            self.networks[role] = network
            self.record("network", network, role)
        self.create_container(
            "database",
            ["storage"],
            database=True,
            environment={
                "POSTGRES_DB": "faultline",
                "POSTGRES_USER": "fl_setup",
                "POSTGRES_PASSWORD": self.passwords["FL_SETUP_PASSWORD"],
                "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256",
            },
        )
        self.create_container(
            "observer",
            ["storage"],
            command=["python", "-c", "import time; time.sleep(900)"],
            environment=self.passwords,
        )
        deadline = time.monotonic() + 40
        while True:
            ready = self.containers["database"].exec_run(
                ["pg_isready", "-U", "fl_setup", "-d", "faultline"]
            )
            if ready.exit_code == 0:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("PostgreSQL readiness deadline exceeded")
            time.sleep(0.2)
        self.call("observer", "prepare", variant, timeout=10)

    def start_application(self, variant):
        self.create_container(
            "application",
            ["client", "storage"],
            environment={
                "FL_APP_PASSWORD": self.passwords["FL_APP_PASSWORD"],
                "FL_VARIANT": variant,
            },
        )
        self.create_container(
            "worker", ["client"], command=["python", "-c", "import time; time.sleep(900)"]
        )
        self.worker_session = WorkerSession(self.client.api, self.containers["worker"].id)
        deadline = time.monotonic() + 15
        while True:
            result = self.call(
                "worker",
                "http",
                json.dumps({"method": "GET", "path": "/health", "timeout_ms": 1000}),
                timeout=4,
            )
            if result.get("status") == 200:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Application readiness deadline exceeded")
            time.sleep(0.1)

    def call(self, role, command, argument, *, timeout):
        if role == "worker" and command == "http" and self.worker_session is not None:
            return self.worker_session.request(json.loads(argument), timeout)
        container = self.containers[role]
        self.assert_owned(container)
        result_queue = queue.Queue(maxsize=1)

        def execute():
            try:
                result_queue.put(
                    container.exec_run(
                        ["python", "-m", "faultline.remote", command, argument], demux=True
                    )
                )
            except Exception as exc:
                result_queue.put(exc)

        threading.Thread(target=execute, daemon=True).start()
        try:
            result = result_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"{role} helper deadline exceeded") from exc
        if isinstance(result, Exception):
            raise RuntimeError(f"{role} helper transport failed") from result
        if result.exit_code != 0:
            # Only a bounded, scrubbed diagnostic; never persist a credential-bearing traceback.
            detail = (result.output[1] or b"").decode(errors="replace").splitlines()
            message = detail[-1][:500] if detail else "no diagnostic"
            for secret in self.passwords.values():
                message = message.replace(secret, "[redacted]")
            raise RuntimeError(f"{role} helper {command} exited {result.exit_code}: {message}")
        stdout, _stderr = result.output
        if stdout is None or len(stdout) > 1048576:
            raise RuntimeError("missing or oversized helper response")
        return json.loads(stdout)

    def snapshot(self, role):
        return self.call("observer", "snapshot", role, timeout=10)

    def drain(self, timeout_ms):
        app = self.containers["application"]
        self.assert_owned(app)
        app.stop(timeout=max(1, timeout_ms // 1000))
        app.reload()
        return app.attrs["State"]["ExitCode"] == 0

    def cleanup(self):
        failures = []
        if self.worker_session is not None:
            self.worker_session.close()
        # Stop the application first; remove networks only after all containers are gone.
        resources = sorted(
            self.bundle.manifest["resources"],
            key=lambda r: (r["kind"] == "network", r["role"] != "application"),
        )
        for record in resources:
            try:
                collection = (
                    self.client.containers
                    if record["kind"] == "container"
                    else self.client.networks
                )
                resource = collection.get(record["id"])
                self.assert_owned(resource)
                if record["kind"] == "container":
                    resource.remove(force=True, v=True)
                else:
                    resource.remove()
            except NotFound:
                continue
            except Exception:
                failures.append(record)
        return {"complete": not failures, "remaining_resources": failures}
