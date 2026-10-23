"""Strict, deliberately small Milestone 1 input and evidence contracts."""

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    account_id: Literal["buyer-a"] = "buyer-a"
    sku: Literal["sku-a"] = "sku-a"
    quantity: Annotated[int, Field(ge=1, le=5)] = 1


class Request(StrictModel):
    method: Literal["POST"]
    path: Literal["/orders"]
    headers: dict[str, str]
    body: Purchase = Field(alias="json")

    @model_validator(mode="after")
    def valid_headers(self):
        if set(self.headers) != {"Idempotency-Key"}:
            raise ValueError("M1 accepts only the Idempotency-Key header; no URLs or secrets")
        key = self.headers["Idempotency-Key"]
        if not 1 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
            raise ValueError("Idempotency-Key must be 1-128 printable ASCII characters")
        return self


class Retry(StrictModel):
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 1
    on: list[Literal["timeout", "connection_error"]] = Field(default_factory=list)
    backoff_ms: list[Annotated[int, Field(ge=0, le=1000)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def backoffs_match(self):
        if len(self.backoff_ms) != self.max_attempts - 1:
            raise ValueError("backoff_ms must contain max_attempts - 1 durations")
        return self


class Operation(StrictModel):
    id: Identifier
    kind: Literal["create_order"]
    logical_id: Identifier
    route: Literal["orders_api"]
    request: Request
    retry: Retry = Field(default_factory=Retry)
    timeout_ms: Annotated[int, Field(ge=50, le=10000)] = 2000

    @model_validator(mode="after")
    def identity_matches(self):
        if self.logical_id != self.request.body.purchase_id:
            raise ValueError("logical_id must equal the purchase_id in M1")
        return self


class Trigger(StrictModel):
    event: Literal["upstream_response_complete"]
    operation_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=5)]
    status: Annotated[int, Field(ge=200, le=299)] = 201


class Fault(StrictModel):
    id: Identifier
    kind: Literal["response_suppress"]
    target: Literal["orders_api"]
    trigger: Trigger
    required: bool = True


class Limits(StrictModel):
    execution_ms: Annotated[int, Field(ge=1, le=300000)] = 60000
    provisioning_ms: Annotated[int, Field(ge=1, le=300000)] = 60000
    drain_ms: Annotated[int, Field(ge=1, le=300000)] = 10000
    observation_ms: Annotated[int, Field(ge=1, le=300000)] = 10000
    verification_ms: Annotated[int, Field(ge=1, le=300000)] = 10000
    cleanup_ms: Annotated[int, Field(ge=1, le=300000)] = 30000
    total_ms: Annotated[int, Field(ge=1, le=900000)] = 300000
    artifact_bytes: Annotated[int, Field(ge=65536, le=52428800)] = 52428800

    @model_validator(mode="after")
    def reserve_cleanup(self):
        if self.total_ms <= self.cleanup_ms:
            raise ValueError("total_ms must exceed cleanup_ms to leave time for experiment work")
        return self


class Scenario(StrictModel):
    schema_version: Literal[1]
    id: Identifier
    adapter_profile: Literal["orders_v1"]
    fixture: Literal["standard_wallet"]
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)]
    concurrency: Literal[1] = 1
    operations: Annotated[list[Operation], Field(min_length=1, max_length=1)]
    faults: Annotated[list[Fault], Field(max_length=1)] = Field(default_factory=list)
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
        if len(self.invariants) != len(INVARIANTS) or set(self.invariants) != set(INVARIANTS):
            raise ValueError("M1 requires all seven order invariants exactly once")
        operation = self.operations[0]
        for fault in self.faults:
            if fault.trigger.operation_id != operation.id:
                raise ValueError("fault references an unknown operation")
            if fault.trigger.attempt > operation.retry.max_attempts:
                raise ValueError("fault references an impossible attempt")
        budget = operation.timeout_ms * operation.retry.max_attempts + sum(
            operation.retry.backoff_ms
        )
        if budget > self.limits.execution_ms:
            raise ValueError("execution budget cannot cover all declared attempts and backoffs")
        return self

    def plan(self) -> dict:
        return {
            "plan_schema_version": 1,
            "implementation_profile": "milestone1",
            "fault_semantics_version": 1,
            "adapter_version": 1,
            "scenario": self.model_dump(by_alias=True),
            "invariant_versions": dict.fromkeys(INVARIANTS, 1),
        }


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


def load_scenario(path: Path) -> Scenario:
    if path.stat().st_size > 1048576:
        raise ValueError("scenario exceeds 1 MiB")
    raw = path.read_text(encoding="utf-8")
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
    return Scenario.model_validate(yaml.load(raw, Loader=UniqueLoader))


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
