"""Runnable order-project subset; parsing never imports an adapter."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, model_validator

from faultline import __version__
from faultline.environment import POSTGRES_IMAGE, RUNTIME_IMAGE
from faultline.models import Identifier, Limits, Scenario, StrictModel, digest, load_document

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
        if self.entry_point == "bundled_orders" and self.package_version != __version__:
            raise ValueError("adapter package_version must exactly match the installed Faultline")
        return self


class Resources(StrictModel):
    memory_bytes: Annotated[int, Field(ge=67108864, le=268435456)]
    cpu_millis: Annotated[int, Field(ge=100, le=1000)]
    pids_limit: Annotated[int, Field(ge=16, le=64)]


class Readiness(StrictModel):
    # Individual timeout/poll overrides are deferred; existing stage budgets apply.
    check: Literal["postgres_ready", "orders_health", "worker_handshake", "observer_snapshot"]


class Service(StrictModel):
    id: Identifier
    role: Literal["application", "postgres", "worker", "observer"]
    image: ImageReference
    ports: Annotated[list[Annotated[int, Field(ge=1, le=65535)]], Field(max_length=1)]
    resources: Resources
    readiness: Readiness
    environment: Annotated[dict[str, str], Field(max_length=0)] = Field(default_factory=dict)
    writable_paths: Annotated[list[str], Field(max_length=0)] = Field(default_factory=list)


class Route(StrictModel):
    id: Literal["orders_api"]
    service: Literal["application"]
    port: Annotated[int, Field(ge=8000, le=8000)]
    base_path: Literal["/"]


class Variant(StrictModel):
    application_image: ImageReference
    environment: Annotated[dict[str, str], Field(max_length=0)] = Field(default_factory=dict)


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
    max_operations: Annotated[int, Field(ge=1, le=1)] | None = None
    max_attempts: Annotated[int, Field(ge=1, le=5)] | None = None
    max_faults: Annotated[int, Field(ge=0, le=1)] | None = None

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
    services: Annotated[list[Service], Field(min_length=4, max_length=4)]
    routes: Annotated[list[Route], Field(min_length=1, max_length=1)]
    # This profile requires explicit known variants. General/default variants are deferred.
    variants: Annotated[
        dict[Literal["unsafe", "correct"], Variant], Field(min_length=1, max_length=2)
    ]
    scenario_paths: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    limits: ProjectLimits = Field(default_factory=ProjectLimits)

    @model_validator(mode="after")
    def supported_topology(self):
        expected = {
            "application": ("application", [8000], "orders_health", None),
            "database": ("postgres", [5432], "postgres_ready", POSTGRES_IMAGE),
            "worker": ("worker", [], "worker_handshake", RUNTIME_IMAGE),
            "observer": ("observer", [], "observer_snapshot", RUNTIME_IMAGE),
        }
        if {service.id for service in self.services} != expected.keys():
            raise ValueError("supported project requires application, database, worker, observer")
        for service in self.services:
            role, ports, readiness, image = expected[service.id]
            if (service.role, service.ports, service.readiness.check) != (role, ports, readiness):
                raise ValueError(
                    "service role, ports, or readiness does not match the order profile"
                )
            if image is not None and service.image != image:
                raise ValueError("database and helper images must match the bundled pinned profile")
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
        counts = {
            "max_operations": len(scenario.operations),
            "max_attempts": sum(operation.retry.max_attempts for operation in scenario.operations),
            "max_faults": len(scenario.faults),
        }
        if any(count > ceilings.get(name, count) for name, count in counts.items()):
            raise ValueError(
                "scenario operation, attempt, or fault count exceeds the project ceiling"
            )
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
