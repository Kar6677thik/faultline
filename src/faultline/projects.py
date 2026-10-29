"""Bounded five-role HTTP/PostgreSQL projects; parsing never imports an adapter."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, model_validator

from faultline import __version__
from faultline.environment import POSTGRES_IMAGE, PROXY_IMAGE, RUNTIME_IMAGE
from faultline.models import (
    Identifier,
    Limits,
    Scenario,
    StrictModel,
    digest,
    load_document,
    safe_path,
)

ImageReference = Annotated[
    str,
    Field(
        max_length=255,
        pattern=r"^(?:sha256:[0-9a-f]{64}|[a-z0-9]+(?:[._/-][a-z0-9]+)*"
        r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64}))$",
    ),
]
StageCeiling = Annotated[int, Field(ge=1, le=300000)]


class AdapterSelection(StrictModel):
    entry_point: Identifier
    contract_version: Annotated[int, Field(ge=1, le=1)]
    package_version: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9.!+_-]+$")
    ]

    @model_validator(mode="after")
    def installed_version(self):
        bundled = {"bundled_orders", "bundled_transfers"}
        if self.entry_point in bundled and self.package_version != __version__:
            raise ValueError("adapter package_version must exactly match the installed Faultline")
        return self


class Resources(StrictModel):
    memory_bytes: Annotated[int, Field(ge=67108864, le=268435456)]
    cpu_millis: Annotated[int, Field(ge=100, le=1000)]
    pids_limit: Annotated[int, Field(ge=16, le=64)]


class Readiness(StrictModel):
    # Stage deadlines may shorten these per-service maxima.
    check: Identifier
    timeout_ms: Annotated[int, Field(ge=1, le=300000)] = 10000
    poll_ms: Annotated[int, Field(ge=1, le=300000)] = 250


class SecretReference(StrictModel):
    secret_ref: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]


EnvironmentValues = dict[
    Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")], str | SecretReference
]


def check_environment(values):
    if len(values) > 64:
        raise ValueError("too many environment values")
    for key, value in values.items():
        if key.startswith(("FL_", "POSTGRES_", "DOCKER_")) or key in {
            "PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_PRELOAD",
        }:
            raise ValueError("engine-owned environment variable cannot be overridden")
        if isinstance(value, str) and (len(value) > 4096 or "\x00" in value):
            raise ValueError("environment value is oversized or contains NUL")


class Service(StrictModel):
    id: Identifier
    role: Literal["application", "postgres", "worker", "observer", "proxy"]
    image: ImageReference
    ports: Annotated[list[Annotated[int, Field(ge=1, le=65535)]], Field(max_length=16)]
    resources: Resources
    readiness: Readiness
    environment: EnvironmentValues = Field(default_factory=dict)
    writable_paths: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def safe_settings(self):
        check_environment(self.environment)
        if len(set(self.writable_paths)) != len(self.writable_paths):
            raise ValueError("duplicate writable path")
        for value in self.writable_paths:
            path = PurePosixPath(value)
            if (
                not path.is_absolute()
                or str(path) != value
                or ".." in path.parts
                or path.parts[1] not in {"data", "cache"}
            ):
                raise ValueError("ephemeral writable paths must be beneath /data or /cache")
        return self


class Route(StrictModel):
    id: Identifier
    service: Identifier
    port: Annotated[int, Field(ge=1, le=65535)]
    base_path: str

    @model_validator(mode="after")
    def path(self):
        safe_path(self.base_path)
        if "?" in self.base_path:
            raise ValueError("route base path cannot contain a query")
        return self


class Variant(StrictModel):
    application_image: ImageReference
    environment: EnvironmentValues = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_environment(self):
        check_environment(self.environment)
        return self


class ProjectLimits(StrictModel):
    # None means the engine hard cap, not the scenario's smaller default.
    execution_ms: StageCeiling | None = None
    provisioning_ms: StageCeiling | None = None
    drain_ms: StageCeiling | None = None
    observation_ms: StageCeiling | None = None
    verification_ms: StageCeiling | None = None
    cleanup_ms: StageCeiling | None = None
    total_ms: Annotated[int, Field(ge=1, le=900000)] | None = None
    artifact_bytes: Annotated[int, Field(ge=65536, le=52428800)] | None = None
    max_operations: Annotated[int, Field(ge=1, le=1000)] | None = None
    max_attempts: Annotated[int, Field(ge=1, le=5000)] | None = None
    max_faults: Annotated[int, Field(ge=0, le=32)] | None = None

    @model_validator(mode="before")
    @classmethod
    def no_explicit_nulls(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("omit a project ceiling instead of setting it to null")
        return value


class Project(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    project_id: Identifier
    adapter: AdapterSelection
    services: Annotated[list[Service], Field(min_length=5, max_length=5)]
    routes: Annotated[list[Route], Field(min_length=1, max_length=16)]
    variants: Annotated[dict[Identifier, Variant], Field(max_length=32)] = Field(
        default_factory=dict
    )
    scenario_paths: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    limits: ProjectLimits = Field(default_factory=ProjectLimits)

    @model_validator(mode="after")
    def supported_topology(self):
        expected = {
            "application": ("application", [8000], "orders_health", None),
            "database": ("postgres", [5432], "postgres_ready", POSTGRES_IMAGE),
            "worker": ("worker", [], "worker_handshake", RUNTIME_IMAGE),
            "observer": ("observer", [], "observer_snapshot", RUNTIME_IMAGE),
            "proxy": ("proxy", [8666, 8474], "proxy_ready", PROXY_IMAGE),
        }
        if {service.role for service in self.services} != {
            "application",
            "postgres",
            "worker",
            "observer",
            "proxy",
        } or len({service.id for service in self.services}) != len(self.services):
            raise ValueError(
                "supported project requires application, database, worker, observer, proxy"
            )
        for service in self.services:
            role, ports, readiness, image = expected[
                "database" if service.role == "postgres" else service.role
            ]
            if service.role != role or len(set(service.ports)) != len(service.ports):
                raise ValueError(
                    "service role, ports, or readiness does not match the order profile"
                )
            if service.role != "application" and service.ports != ports:
                raise ValueError("dependency/helper ports must implement the engine contract")
        application = next(s for s in self.services if s.role == "application")
        if len({route.id for route in self.routes}) != len(self.routes):
            raise ValueError("duplicate route identifier")
        for route in self.routes:
            if route.service != application.id or route.port not in application.ports:
                raise ValueError("route must use a declared application port")
        if not self.variants:
            self.variants = {"default": Variant(application_image=application.image)}
        if len(set(self.scenario_paths)) != len(self.scenario_paths):
            raise ValueError("duplicate scenario_paths")
        for value in self.scenario_paths:
            relative_path(value)
        return self

    def select_variant(self, requested: str | None) -> str:
        if requested is None and len(self.variants) == 1:
            return next(iter(self.variants))
        if requested not in self.variants:
            raise ValueError("choose --variant from the project's declared variants")
        return requested

    def scenario(self, raw: dict) -> Scenario:
        """Clip omitted defaults; reject explicit values over ceilings instead of rewriting them."""
        if not isinstance(raw, dict):
            raise ValueError("scenario must contain a mapping")
        explicit = raw.get("limits", {})
        if not isinstance(explicit, dict):
            raise ValueError("scenario limits must contain a mapping")
        defaults = Limits().model_dump()
        ceilings = self.limits.model_dump(exclude_none=True)
        for name, ceiling in ceilings.items():
            if name not in defaults:
                continue
            if name in explicit:
                if type(explicit[name]) is int and explicit[name] > ceiling:
                    raise ValueError(f"scenario limits.{name} exceeds the project ceiling")
            else:
                defaults[name] = min(defaults[name], ceiling)
        scenario = Scenario.model_validate({**raw, "limits": {**defaults, **explicit}})
        if any(
            s.readiness.timeout_ms > scenario.limits.provisioning_ms
            or s.readiness.poll_ms > s.readiness.timeout_ms
            for s in self.services
        ):
            raise ValueError("service readiness must fit provisioning budget")
        return scenario

    def configuration(self):
        return self.model_dump(exclude_none=True)

    def identity(self):
        return {"project_id": self.project_id, "configuration_hash": digest(self.configuration())}


def relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("scenario paths must be plain project-relative paths")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("scenario paths cannot be absolute or contain empty/traversal components")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("scenario paths must be relative to the project")
    return path


@dataclass(frozen=True)
class LoadedProject:
    project: Project
    root: Path

    def scenario_path(self, value: str) -> Path:
        relative = relative_path(value)
        candidate = self.root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError("project scenario paths cannot traverse symlinks")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError("scenario must be a file within the project root")
        return resolved

    def load_scenario(self, value: str) -> Scenario:
        return self.project.scenario(load_document(self.scenario_path(value)))


def load_project(path: Path) -> LoadedProject:
    # Resolve the selected parent once; do not follow a symlinked manifest itself.
    project = Project.model_validate(load_document(path))
    loaded = LoadedProject(project, path.parent.resolve(strict=True))
    for value in project.scenario_paths:
        loaded.scenario_path(value)
    return loaded
