"""Strict bounded scenario, materialized workload, and evidence contracts."""

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    RootModel,
    model_validator,
)

INVARIANTS = (
    "orders.at_most_one_debit",
    "orders.purchase_atomicity",
    "orders.wallet_conservation",
    "orders.inventory_conservation",
    "orders.confirmation_intent",
    "orders.idempotency_contract",
    "orders.completed_purchase",
)
EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3, "ERROR": 4}
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Purchase(StrictModel):
    purchase_id: Identifier
    # Membership in the selected fixture is checked by the adapter.
    account_id: Identifier = "buyer-a"
    sku: Identifier = "sku-a"
    quantity: Annotated[int, Field(ge=1, le=2147483647)] = 1


def safe_path(value):
    decoded = value
    for _ in range(4):
        following = unquote(decoded)
        if following == decoded:
            break
        decoded = following
    parsed = urlsplit(decoded)
    query_keys = [key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)]
    if len(query_keys) != len(set(query_keys)):
        raise ValueError("repeated query keys are unsupported")
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "\\" in decoded
        or "%" in decoded
        or any(ord(character) < 32 for character in decoded)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("request path must remain inside its internal route")
    return parsed.path


class Body(RootModel[JsonValue]):
    model_config = ConfigDict(strict=True)

    @model_validator(mode="after")
    def bounded_json(self):
        stack = [self.root]
        while stack:
            item = stack.pop()
            if type(item) is float or (type(item) is int and not -(2**63) <= item < 2**63):
                raise ValueError("payload numbers must be signed 64-bit integers")
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        if len(canonical_bytes(self.root)) > 65536:
            raise ValueError("request body exceeds 64 KiB")
        return self


class Request(StrictModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: Annotated[str, Field(max_length=2048)]
    headers: dict[str, str] = Field(default_factory=dict)
    body: Body = Field(alias="json")

    @model_validator(mode="after")
    def valid_headers(self):
        safe_path(self.path)
        normalized = [name.lower() for name in self.headers]
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate case-insensitive header")
        for name, value in self.headers.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
                or name.lower().startswith("x-faultline-")
                or name.lower()
                in {
                    "host",
                    "authorization",
                    "proxy-authorization",
                    "connection",
                    "content-length",
                    "transfer-encoding",
                }
                or not value.isascii()
                or not value.isprintable()
                or len(value) > 1024
            ):
                raise ValueError("invalid, reserved, or credential-bearing header")
        return self


class Retry(StrictModel):
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 1
    on: list[
        Literal[
            "timeout",
            "connection_error",
            "http_429",
            "http_500",
            "http_502",
            "http_503",
            "http_504",
        ]
    ] = Field(default_factory=list)
    backoff_ms: list[Annotated[int, Field(ge=0, le=300000)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def backoffs_match(self):
        if len(self.backoff_ms) != self.max_attempts - 1:
            raise ValueError("backoff_ms must contain max_attempts - 1 durations")
        return self


class Operation(StrictModel):
    id: Identifier
    kind: Identifier
    logical_id: Identifier
    route: Identifier
    request: Request
    retry: Retry = Field(default_factory=Retry)
    timeout_ms: Annotated[int, Field(ge=50, le=30000)] = 2000
    after: list[Identifier] = Field(default_factory=list)
    after_faults: list[Identifier] = Field(default_factory=list)
    barrier: Identifier | None = None
    expectation: Identifier = "success"


class Trigger(StrictModel):
    event: (
        Literal[
            "upstream_response_complete",
            "attempt_dispatched",
            "client_outcome",
            "adapter_checkpoint",
        ]
        | None
    ) = None
    operation_id: Identifier | None = None
    attempt: Annotated[int, Field(ge=1, le=5)] | None = None
    status: Annotated[int, Field(ge=200, le=299)] | None = None
    offset_ms: Annotated[int, Field(ge=0, le=300000)] | None = None
    # Only with event "adapter_checkpoint"; the adapter must declare the checkpoint.
    checkpoint: (
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")] | None
    ) = None

    @model_validator(mode="after")
    def one_form(self):
        if (self.checkpoint is None) != (self.event != "adapter_checkpoint"):
            raise ValueError("adapter_checkpoint triggers require exactly one checkpoint name")
        if self.offset_ms is not None:
            if any(
                v is not None for v in (self.event, self.operation_id, self.attempt, self.status)
            ):
                raise ValueError("trigger must be an offset or an event")
        elif self.event is None or self.operation_id is None or self.attempt is None:
            raise ValueError("event trigger requires event, operation_id and attempt")
        if self.status is not None and self.event != "upstream_response_complete":
            raise ValueError("only a response trigger accepts status")
        return self


class Fault(StrictModel):
    id: Identifier
    kind: Literal["response_suppress", "proxy_latency", "proxy_disconnect", "process_restart"]
    target: Identifier
    trigger: Trigger
    required: bool = True
    latency_ms: Annotated[int, Field(ge=1, le=30000)] | None = None
    direction: Literal["upstream", "downstream"] | None = None
    duration_ms: Annotated[int, Field(ge=1, le=60000)] | None = None
    downtime_ms: Annotated[int, Field(ge=0, le=60000)] | None = None

    @model_validator(mode="after")
    def kind_contract(self):
        values = {
            k
            for k in ("latency_ms", "direction", "duration_ms", "downtime_ms")
            if getattr(self, k) is not None
        }
        expected = {
            "response_suppress": set(),
            "proxy_latency": {"latency_ms", "direction", "duration_ms"},
            "proxy_disconnect": {"duration_ms"},
            "process_restart": {"downtime_ms"},
        }
        if values != expected[self.kind]:
            raise ValueError("fault parameters do not match the fault kind")
        target = "application" if self.kind == "process_restart" else "database"
        if self.kind != "response_suppress" and self.target != target:
            raise ValueError("fault target is not a registered route, binding, or application")
        if self.kind == "response_suppress":
            if self.trigger.event != "upstream_response_complete" or self.trigger.status is None:
                raise ValueError("suppression requires an exact successful response status")
        elif self.trigger.event == "upstream_response_complete":
            raise ValueError("dependency/lifecycle faults cannot trigger on upstream responses")
        if self.trigger.event == "adapter_checkpoint" and self.kind != "process_restart":
            raise ValueError("only process_restart may use an adapter checkpoint trigger")
        return self


class Limits(StrictModel):
    execution_ms: Annotated[int, Field(ge=1, le=300000)] = 60000
    provisioning_ms: Annotated[int, Field(ge=1, le=300000)] = 60000
    # Defaults leave room for helper start-up on a contended host (see decision L-061).
    drain_ms: Annotated[int, Field(ge=1, le=300000)] = 30000
    observation_ms: Annotated[int, Field(ge=1, le=300000)] = 30000
    verification_ms: Annotated[int, Field(ge=1, le=300000)] = 10000
    cleanup_ms: Annotated[int, Field(ge=1, le=300000)] = 30000
    total_ms: Annotated[int, Field(ge=1, le=900000)] = 300000
    artifact_bytes: Annotated[int, Field(ge=65536, le=52428800)] = 52428800
    max_operations: Annotated[int, Field(ge=1, le=1000)] = 1000
    max_attempts: Annotated[int, Field(ge=1, le=5000)] = 5000
    max_faults: Annotated[int, Field(ge=0, le=32)] = 32

    @model_validator(mode="after")
    def reserve_cleanup(self):
        if self.total_ms <= self.cleanup_ms:
            raise ValueError("total_ms must exceed cleanup_ms to leave time for experiment work")
        return self


class Barrier(StrictModel):
    id: Identifier
    members: Annotated[list[Identifier], Field(min_length=1, max_length=1000)]


class GeneratorParameters(StrictModel):
    quantity_min: Annotated[int, Field(ge=1, le=5)] = 1
    quantity_max: Annotated[int, Field(ge=1, le=5)] = 1

    @model_validator(mode="after")
    def ordered(self):
        if self.quantity_min > self.quantity_max:
            raise ValueError("generator quantity_min exceeds quantity_max")
        return self


class Generator(StrictModel):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
    kind: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
    count: Annotated[int, Field(ge=1, le=1000)]
    parameters: dict[str, JsonValue]

    def expand(self, seed):
        if self.kind != "orders.purchases":
            raise ValueError("generator must be materialized by its registered adapter")
        parameters = GeneratorParameters.model_validate(self.parameters)
        # A versioned counter hash avoids Python random/global-state/version dependence.
        operations = []
        for index in range(self.count):
            identity = f"{self.id}-{index + 1}"
            number = int(digest(["sha256-counter-v1", seed, self.id, index]), 16)
            quantity = parameters.quantity_min + number % (
                parameters.quantity_max - parameters.quantity_min + 1
            )
            operations.append(
                Operation.model_validate(
                    {
                        "id": identity,
                        "kind": "create_order",
                        "logical_id": identity,
                        "route": "orders_api",
                        "request": {
                            "method": "POST",
                            "path": "/orders",
                            "headers": {"Idempotency-Key": identity},
                            "json": {
                                "purchase_id": identity,
                                "account_id": "buyer-a",
                                "sku": "sku-a",
                                "quantity": quantity,
                            },
                        },
                    }
                )
            )
        return operations


class Scenario(StrictModel):
    _adapter_plan: dict = PrivateAttr(default_factory=dict)
    _generated: list[Operation] | None = PrivateAttr(default=None)
    _generation_identity: list[dict] = PrivateAttr(default_factory=list)
    schema_version: Literal[1]
    id: Identifier
    adapter_profile: Identifier
    fixture: Identifier
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)]
    concurrency: Annotated[int, Field(ge=1, le=32)] = 1
    operations: Annotated[list[Operation], Field(max_length=1000)]
    faults: Annotated[list[Fault], Field(max_length=32)] = Field(default_factory=list)
    barriers: Annotated[list[Barrier], Field(max_length=1000)] = Field(default_factory=list)
    generators: Annotated[list[Generator], Field(max_length=1000)] = Field(default_factory=list)
    invariants: list[str]
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="before")
    @classmethod
    def literal_integers_are_not_booleans(cls, value):
        if isinstance(value, dict) and any(
            type(value.get(field)) is bool for field in ("schema_version", "concurrency")
        ):
            raise ValueError("schema_version and concurrency must be integers, not booleans")
        return value

    @model_validator(mode="after")
    def supported_plan(self):
        if not self.invariants or len(set(self.invariants)) != len(self.invariants):
            raise ValueError("invariants must be nonempty and unique")
        if (
            len(self.operations) + sum(g.count for g in self.generators)
            > self.limits.max_operations
        ):
            raise ValueError("operation count exceeds limits")
        if self._generated is None and any(g.kind != "orders.purchases" for g in self.generators):
            # Adapter registration is resolved after inert YAML parsing, before Docker.
            # Expanded graph/attempt checks run again when materialization completes.
            return self
        operations = self.all_operations
        if not operations or len(operations) > self.limits.max_operations:
            raise ValueError("operation count exceeds limits or is empty")
        if sum(op.retry.max_attempts for op in operations) > self.limits.max_attempts:
            raise ValueError("potential attempt count exceeds limits")
        if len(self.faults) > self.limits.max_faults:
            raise ValueError("fault count exceeds limits")
        for items in (operations, self.faults, self.barriers, self.generators):
            if len({item.id for item in items}) != len(items):
                raise ValueError("duplicate identifier")
        by_id = {op.id: op for op in operations}
        fault_ids = {f.id for f in self.faults}
        barrier_ids = {b.id for b in self.barriers}
        graph = {("op", op.id): set() for op in operations}
        for op in operations:
            if len(set(op.after)) != len(op.after) or len(set(op.after_faults)) != len(
                op.after_faults
            ):
                raise ValueError("duplicate dependency")
            if not set(op.after) <= by_id.keys() or not set(op.after_faults) <= fault_ids:
                raise ValueError("unknown operation or fault dependency")
            if op.barrier is not None and op.barrier not in barrier_ids:
                raise ValueError("unknown barrier")
            graph["op", op.id].update(("op", dep) for dep in op.after)
            graph["op", op.id].update(("fault", dep) for dep in op.after_faults)
        for fault in self.faults:
            if fault.trigger.offset_ms is not None:
                if fault.trigger.offset_ms >= self.limits.execution_ms:
                    raise ValueError("fault offset cannot occur within execution budget")
                graph["fault", fault.id] = set()
                continue
            if fault.trigger.operation_id not in by_id:
                raise ValueError("fault references an unknown operation")
            operation = by_id[fault.trigger.operation_id]
            if fault.kind == "response_suppress" and fault.target != operation.route:
                raise ValueError("response suppression target must match operation route")
            if fault.trigger.attempt > operation.retry.max_attempts:
                raise ValueError("fault references an impossible attempt")
            graph["fault", fault.id] = {("op", operation.id)}
        targets = [
            (f.trigger.operation_id, f.trigger.attempt)
            for f in self.faults
            if f.kind == "response_suppress"
        ]
        if len(set(targets)) != len(targets):
            raise ValueError("overlapping response suppression faults")
        for target in ("database", "application"):
            faults = [f for f in self.faults if f.target == target]
            # No uncertain event overlap. Offset intervals on the same target must be disjoint.
            if len(faults) > 1:
                if any(f.trigger.offset_ms is None for f in faults):
                    raise ValueError(
                        "cannot prove nonoverlap for event-driven faults on same target"
                    )
                raise ValueError("multiple faults on a binding require explicit healing gates")
        for barrier in self.barriers:
            members = set(barrier.members)
            if len(members) != len(barrier.members) or not members <= by_id.keys():
                raise ValueError("invalid barrier members")
            if {op.id for op in operations if op.barrier == barrier.id} != members:
                raise ValueError("barrier membership must agree in both directions")
            dependencies = set().union(*(graph["op", member] for member in members))
            for member in members:
                graph["op", member].update(dependencies)
        # Iterative elimination handles the 1000-operation cap without recursion limits.
        while graph:
            ready = {key for key, deps in graph.items() if not deps}
            if not ready:
                raise ValueError("cyclic operation, barrier, or fault dependency")
            graph = {key: deps - ready for key, deps in graph.items() if key not in ready}
        budget = max(
            op.timeout_ms * op.retry.max_attempts + sum(op.retry.backoff_ms) for op in operations
        )
        if budget > self.limits.execution_ms:
            raise ValueError("execution budget cannot cover all declared attempts and backoffs")
        return self

    @property
    def all_operations(self):
        if self._generated is not None:
            return [*self.operations, *self._generated]
        return [
            *self.operations,
            *(op for generator in self.generators for op in generator.expand(self.seed)),
        ]

    def plan(self) -> dict:
        scenario = self.model_dump(by_alias=True)
        scenario["operations"] = [op.model_dump(by_alias=True) for op in self.all_operations]
        scenario["generators"] = []
        return {
            "plan_schema_version": 1,
            "implementation_profile": "experiment_engine_v1",
            "canonicalization_version": 1,
            "generators": self._generation_identity
            or [
                {**g.model_dump(), "version": 1, "prng": "sha256-counter-v1"}
                for g in self.generators
            ],
            "requests": [
                {
                    "operation_id": op.id,
                    "body_base64": base64.b64encode(
                        canonical_bytes(op.request.body.model_dump())
                    ).decode(),
                    "body_hash": digest(op.request.body.model_dump()),
                    "attempt_ids": [f"{op.id}/{i}" for i in range(1, op.retry.max_attempts + 1)],
                }
                for op in self.all_operations
            ],
            "fault_semantics_version": 1,
            "adapter_version": 1,
            "adapter_contract": self._adapter_plan,
            "scenario": scenario,
            "invariant_versions": self._adapter_plan.get(
                "invariants", dict.fromkeys(self.invariants, 1)
            ),
        }

    @classmethod
    def from_plan(cls, plan: dict, project=None) -> "Scenario":
        """Rebuild the saved materialized workload; generators are never re-expanded."""
        if (
            plan.get("plan_schema_version") != 1
            or plan.get("canonicalization_version") != 1
            or plan.get("fault_semantics_version") != 1
            or plan.get("implementation_profile") != "experiment_engine_v1"
        ):
            raise ValueError("saved plan uses an unsupported plan, fault, or profile version")
        raw = plan["scenario"]
        if raw.get("generators"):
            raise ValueError("saved plan contains unexpanded generators")
        scenario = project.scenario(raw) if project is not None else cls.model_validate(raw)
        scenario.keep_generation_identity(plan["generators"])
        return scenario

    def keep_generation_identity(self, identities):
        # Saved generator provenance stays in the plan even though its values are now literal.
        if not self.generators:
            self._generation_identity = json.loads(canonical_bytes(identities))

    @property
    def checkpoints(self) -> list[str]:
        """Instrumented checkpoints this plan relies on, for application configuration."""
        return sorted(
            {f.trigger.checkpoint for f in self.faults if f.trigger.checkpoint is not None}
        )

    def bind_adapter_plan(self, value):
        self._adapter_plan = json.loads(canonical_bytes(value))

    def materialize_generators(self, generated, identities):
        self._generated = generated
        self._generation_identity = identities
        self.supported_plan()


class UniqueLoader(yaml.SafeLoader):
    pass


# YAML 1.1 treats the retry key "on" as True. Accept only JSON-style boolean literals.
UniqueLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in entries if tag != "tag:yaml.org,2002:bool"]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
UniqueLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), "tf")


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ValueError("YAML mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_document(path: Path) -> dict:
    """Shared bounded YAML loader; a FIFO must not turn validation into a blocking read."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1048576:
            raise ValueError("configuration must be a regular file of at most 1 MiB")
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("configuration exceeds 1 MiB")
    try:
        return parse_document(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        # Parser errors can otherwise print entire lines containing rejected secret values.
        raise ValueError("Invalid UTF-8 YAML syntax or unsupported YAML tag") from exc


def parse_document(raw: str) -> dict:
    depth = 0
    for event in yaml.parse(raw):
        if getattr(event, "anchor", None) or isinstance(event, yaml.AliasEvent):
            raise ValueError("YAML anchors and aliases are unsupported")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > 32:
                raise ValueError("YAML exceeds maximum nesting depth")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
    value = yaml.load(raw, Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError("configuration must contain a mapping")
    try:
        canonical_bytes(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("configuration must contain finite JSON-compatible values") from exc
    return value


def load_scenario(path: Path) -> Scenario:
    return Scenario.model_validate(load_document(path))


def verdict(findings: list[dict], diagnostics: list[dict], coverage_complete: bool) -> str:
    if any(item["severity"] == "error" for item in diagnostics):
        return "ERROR"
    if any(item["status"] == "FAIL" for item in findings):
        return "FAIL"
    if not coverage_complete or any(item["status"] == "UNKNOWN" for item in findings):
        return "INCONCLUSIVE"
    if not findings:
        return "INCONCLUSIVE"
    return "PASS"
