"""Only locally created, manifest-recorded resources can be mutated or deleted."""

import json
import os
import platform
import queue
import secrets
import threading
import time
from uuid import uuid4

import docker
from docker.errors import NotFound

from faultline.ownership import validate_intents, validate_resources
from faultline.worker import WorkerSession

POSTGRES_IMAGE = "postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb"
RUNTIME_IMAGE = "faultline-runtime:0.1.0"
PROXY_IMAGE = (
    "ghcr.io/shopify/toxiproxy@sha256:"
    "9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e"
)
LABEL_PREFIX = "io.faultline"


class PreflightError(ValueError):
    pass


def resolve_environment(values):
    result = {}
    for key, value in values.items():
        if isinstance(value, str):
            result[key] = value
        else:
            if value.secret_ref not in os.environ:
                raise PreflightError("a declared environment secret reference is unavailable")
            result[key] = os.environ[value.secret_ref]
        if len(result[key]) > 4096 or "\x00" in result[key]:
            raise PreflightError("invalid environment value")
    return result


def client_and_preflight(*, require_images=True, project=None, variant=None):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise PreflightError(
            "Execution requires Linux x86-64; use Debian/Ubuntu in WSL2 on Windows"
        )
    host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if host != "unix:///var/run/docker.sock":
        raise PreflightError("Only the local /var/run/docker.sock daemon is supported")
    client = docker.DockerClient(base_url=host, timeout=15)
    try:
        info = client.info()
        if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
            raise PreflightError("Docker must run Linux x86-64 containers")
        references = (
            {"runtime": RUNTIME_IMAGE, "postgres": POSTGRES_IMAGE, "proxy": PROXY_IMAGE}
            if require_images
            else {}
        )
        if project is not None:
            references = {}
            variant = project.select_variant(variant)
            memory = sum(service.resources.memory_bytes for service in project.services)
            cpu = sum(service.resources.cpu_millis for service in project.services)
            if memory > info.get("MemTotal", 0) or cpu > info.get("NCPU", 0) * 1000:
                raise PreflightError("project resource allocations exceed the Docker host budget")
            references["application"] = project.variants[variant].application_image
            for service in project.services:
                if service.role != "application":
                    references[service.role] = service.image
                resolve_environment(service.environment)
            resolve_environment(project.variants[variant].environment)
        images = {}
        for role, reference in references.items():
            image = client.images.get(reference)
            if image.attrs.get("Os") != "linux" or image.attrs.get("Architecture") != "amd64":
                raise PreflightError("selected images must be Linux x86-64")
            images[role] = image.id
        result = {
            "os": platform.system(),
            "architecture": platform.machine(),
            "kernel": platform.release(),
            "docker": client.version()["Version"],
            "images": images,
        }
        if project is not None:
            result.update(
                project_configuration_hash=project.identity()["configuration_hash"],
                variant=variant,
                image_references=references,
            )
    except NotFound as exc:
        client.close()
        raise PreflightError(
            "Images missing locally; build/pull the declared images first"
        ) from exc
    except BaseException:
        client.close()
        raise
    return client, result


class Environment:
    def __init__(self, client, bundle, images: dict, project=None, *, persist_binding=None):
        self.client, self.bundle, self.images = client, bundle, images
        self.project = project
        self.services = (
            {
                "database" if service.role == "postgres" else service.role: service
                for service in project.services
            }
            if project
            else {}
        )
        self.routes = {route.id: route for route in project.routes} if project else {}
        self.labels = {
            f"{LABEL_PREFIX}.managed": "true",
            f"{LABEL_PREFIX}.project": bundle.manifest.get("project_id", "orders-m1"),
            f"{LABEL_PREFIX}.run": bundle.run_id,
            f"{LABEL_PREFIX}.nonce": bundle.manifest["ownership_nonce"],
        }
        self.containers = {}
        self.networks = {}
        self.worker_session = None
        self.cleanup_remaining = []
        self.persist_binding = persist_binding
        self.passwords = {
            f"FL_{role.upper()}_PASSWORD": secrets.token_urlsafe(32)
            for role in ("setup", "app", "observer")
        }
        self.secret_values = list(self.passwords.values())
        if project:
            settings = [service.environment for service in project.services]
            settings.extend(variant.environment for variant in project.variants.values())
            self.secret_values.extend(
                os.environ[value.secret_ref]
                for values in settings
                for value in values.values()
                if not isinstance(value, str) and value.secret_ref in os.environ
            )

    def record(self, kind, resource, role):
        self._save_record({"kind": kind, "id": resource.id, "role": role})

    def _save_record(self, record):
        previous = self.bundle.manifest["resources"]
        validate_resources([*previous, record])
        if self.persist_binding is not None:
            # Recovery writes a separate durable binding before granting mutation rights.
            self.persist_binding(record)
            self.bundle.manifest["resources"] = [*previous, record]
            return
        self.bundle.manifest["resources"] = [*previous, record]
        try:
            self.bundle.save_manifest()
        except BaseException:
            self.bundle.manifest["resources"] = previous
            raise

    def begin_creation(self, kind, role):
        intent = {
            "kind": kind,
            "role": role,
            "name": f"fl-{self.bundle.run_id}-{role}",
            "token": str(uuid4()),
        }
        previous = self.bundle.manifest.get("creation_intents", [])
        intents = [*previous, intent]
        validate_intents(intents, self.bundle.run_id)
        self.bundle.manifest["creation_intents"] = intents
        # No Docker create call is allowed before this durable write succeeds.
        try:
            self.bundle.save_manifest()
        except BaseException:
            # No create has been attempted; ordinary error handling can forget this intent.
            self.bundle.manifest["creation_intents"] = previous
            raise
        return intent

    def creation_labels(self, intent):
        return {
            **self.labels,
            f"{LABEL_PREFIX}.creation": intent["token"],
            f"{LABEL_PREFIX}.kind": intent["kind"],
            f"{LABEL_PREFIX}.role": intent["role"],
        }

    def _assert_labels(self, resource, intent=None):
        resource.reload()
        labels = resource.attrs.get("Labels") or resource.attrs.get("Config", {}).get("Labels", {})
        expected = self.creation_labels(intent) if intent else self.labels
        if any(labels.get(key) != value for key, value in expected.items()):
            raise RuntimeError("resource ownership mismatch; refusing mutation")
        if intent:
            name = resource.attrs.get("Name", "")
            if intent["kind"] == "container":
                name = name.removeprefix("/")
            if name != intent["name"]:
                raise RuntimeError("resource creation name mismatch; refusing mutation")

    def assert_owned(self, resource):
        record = next(
            (r for r in self.bundle.manifest["resources"] if r["id"] == resource.id), None
        )
        if record is None:
            raise RuntimeError("resource missing from ownership manifest")
        intent = next(
            (
                i
                for i in self.bundle.manifest.get("creation_intents", [])
                if (i["kind"], i["role"]) == (record["kind"], record["role"])
            ),
            None,
        )
        self._assert_labels(resource, intent)

    def pending_creations(self):
        bound = {(r["kind"], r["role"]) for r in self.bundle.manifest["resources"]}
        return [
            i
            for i in self.bundle.manifest.get("creation_intents", [])
            if (i["kind"], i["role"]) not in bound
        ]

    def reconcile_creations(self):
        unresolved = []
        for intent in self.pending_creations():
            try:
                collection = (
                    self.client.containers
                    if intent["kind"] == "container"
                    else self.client.networks
                )
                resource = collection.get(intent["name"])
                self._assert_labels(resource, intent)
                self._save_record(
                    {"kind": intent["kind"], "role": intent["role"], "id": resource.id}
                )
            except Exception:
                # Even NotFound is uncertain: an interrupted daemon request may finish later.
                # Once an ID is bound, ordinary ID-based cleanup can establish absence.
                unresolved.append(intent)
        return unresolved

    def create_container(
        self, role, networks, *, command=None, environment=None, database=False, address=None
    ):
        resources = self.services[role].resources if role in self.services else None
        image = (
            self.images["postgres"]
            if database
            else self.images.get(role, self.images.get("runtime"))
        )
        if role == "application":
            image = self.images.get("application", image)
        if role == "proxy":
            image = self.images["proxy"]
        if role in {"observer", "worker"}:
            image = self.images.get(role, image)
        tmpfs = {"/tmp": "rw,noexec,nosuid,size=16777216"}
        if role in self.services:
            tmpfs.update(
                {
                    path: "rw,noexec,nosuid,size=16777216,uid=10001,gid=10001"
                    for path in self.services[role].writable_paths
                }
            )
            environment = {
                **resolve_environment(self.services[role].environment),
                **(environment or {}),
            }
        if database:
            tmpfs.update(
                {
                    "/var/lib/postgresql/data": "rw,nosuid,size=134217728,uid=70,gid=70,mode=0700",
                    "/var/run/postgresql": "rw,nosuid,size=16777216,uid=70,gid=70",
                }
            )
        intent = self.begin_creation("container", role)
        container = self.client.containers.create(
            image,
            command=command,
            name=intent["name"],
            hostname=role,
            environment=environment or {},
            labels=self.creation_labels(intent),
            network=self.networks[networks[0]].id,
            networking_config={
                self.networks[networks[0]].id: self.client.api.create_endpoint_config(
                    aliases=[role, "proxy-control"] if role == "proxy" else [role],
                    **({"ipv4_address": address} if address else {}),
                )
            },
            user="postgres" if database else "10001:10001",
            read_only=True,
            tmpfs=tmpfs,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit=resources.memory_bytes if resources else 268435456,
            nano_cpus=resources.cpu_millis * 1000000 if resources else 1000000000,
            pids_limit=resources.pids_limit if resources else 64,
            restart_policy={"Name": "no"},
            detach=True,
            log_config=docker.types.LogConfig(
                type="local", config={"max-size": "1m", "max-file": "1", "compress": "false"}
            ),
        )
        self.record("container", container, role)
        self.containers[role] = container
        self.assert_owned(container)
        # Attach aliases before startup. Docker also resolves the service hostname.
        for network in networks[1:]:
            self.assert_owned(self.networks[network])
            self.networks[network].connect(container, aliases=[role])
        self.assert_owned(container)
        container.start()
        return container

    def provision(self):
        for role in ("client", "dependency", "storage", "control"):
            intent = self.begin_creation("network", role)
            network = self.client.networks.create(
                intent["name"],
                internal=True,
                labels=self.creation_labels(intent),
            )
            self.networks[role] = network
            self.record("network", network, role)
            self.assert_owned(network)
        self.proxy_address = "proxy-control"
        self.create_container(
            "proxy",
            ["control", "dependency", "storage"],
            command=[f"-host={self.proxy_address}", "-port=8474"],
        )
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
            ["storage", "control"],
            command=["python", "-c", "import time; time.sleep(900)"],
            environment={**self.passwords, "FL_PROXY_ADMIN": self.proxy_address},
        )
        readiness = self.services["database"].readiness if self.services else None
        deadline = time.monotonic() + (readiness.timeout_ms / 1000 if readiness else 40)
        while True:
            ready = self.containers["database"].exec_run(
                ["pg_isready", "-U", "fl_setup", "-d", "faultline"]
            )
            if ready.exit_code == 0:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("PostgreSQL readiness deadline exceeded")
            time.sleep(readiness.poll_ms / 1000 if readiness else 0.2)
        self.call("observer", "proxy", json.dumps({"action": "prepare"}), timeout=10)

    def start_application(self, variant):
        variant_environment = (
            resolve_environment(self.project.variants[variant].environment) if self.project else {}
        )
        self.create_container(
            "application",
            ["client", "dependency"],
            environment={
                **variant_environment,
                "FL_APP_PASSWORD": self.passwords["FL_APP_PASSWORD"],
                "FL_VARIANT": variant,
                "FL_DB_HOST": "proxy",
                "FL_DB_PORT": "8666",
            },
        )
        self.create_container(
            "worker",
            ["client", "control"],
            command=["python", "-c", "import time; time.sleep(900)"],
            environment={"FL_PROXY_ADMIN": self.proxy_address},
        )
        self.worker_session = WorkerSession(self.client.api, self.containers["worker"].id)

    def wait_application_ready(self):
        readiness = self.services["application"].readiness if self.services else None
        deadline = time.monotonic() + (readiness.timeout_ms / 1000 if readiness else 15)
        while True:
            result = self.call(
                "worker",
                "http",
                json.dumps(
                    {
                        "method": "GET",
                        "path": "/health",
                        "timeout_ms": 1000,
                        "port": next(iter(self.routes.values())).port if self.routes else 8000,
                    }
                ),
                timeout=4,
            )
            if result.get("status") == 200:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Application readiness deadline exceeded")
            time.sleep(readiness.poll_ms / 1000 if readiness else 0.1)

    def call(self, role, command, argument, *, timeout, module="faultline.remote"):
        # Trusted adapters may select their installed helper module, never shell text.
        if not all(part.isidentifier() for part in module.split(".")):
            raise ValueError("helper must be an installed Python module name")
        if role == "worker" and command == "http" and self.worker_session is not None:
            request = json.loads(argument)
            route = request.pop("route", None)
            if self.routes and route is not None:
                if route not in self.routes:
                    raise ValueError("unknown internal route")
                request["port"] = self.routes[route].port
            return self.worker_session.request(request, timeout)
        container = self.containers[role]
        self.assert_owned(container)
        result_queue = queue.Queue(maxsize=1)

        def execute():
            try:
                result_queue.put(
                    container.exec_run(["python", "-m", module, command, argument], demux=True)
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
            for secret in self.secret_values:
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
        before = self.call("observer", "registry", "", timeout=5)
        self.bundle.event("inflight_before_drain", before)
        app.stop(timeout=max(1, timeout_ms // 1000))
        app.reload()
        if app.attrs["State"]["Running"]:
            return False
        evidence = self.call("observer", "registry", "", timeout=5)
        self.bundle.event("inflight_after_exit", evidence)
        if evidence["application_sessions"] != 0 or any(
            row["session_alive"] for row in evidence["registrations"]
        ):
            return False
        cleared = self.call("observer", "clear_registry", "", timeout=5)
        self.bundle.event("inflight_stale_cleared", cleared)
        return app.attrs["State"]["ExitCode"] == 0

    def cleanup(self):
        self.cleanup_remaining = [*self.bundle.manifest["resources"], *self.pending_creations()]
        failures = self.reconcile_creations()
        # Stop the application first; remove networks only after all containers are gone.
        resources = sorted(
            self.bundle.manifest["resources"],
            key=lambda r: (r["kind"] == "network", r["role"] != "application"),
        )
        # Keep unresolved IDs visible even if a stage alarm interrupts a Docker call.
        self.cleanup_remaining = [*resources, *failures]
        if self.worker_session is not None:
            try:
                self.worker_session.close()
            except Exception:
                # Removing the worker below also terminates its remaining helper process.
                pass
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
                pass
            except Exception:
                failures.append(record)
                continue
            self.cleanup_remaining.remove(record)
        return {"complete": not failures, "remaining_resources": failures}
