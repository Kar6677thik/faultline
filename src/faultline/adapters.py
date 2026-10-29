"""Checked trusted adapter hooks; ownership and deadlines remain engine responsibilities."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from faultline import __version__, verifier
from faultline.models import (
    INVARIANTS,
    Identifier,
    Operation,
    Purchase,
    Scenario,
    StrictModel,
    canonical_bytes,
    digest,
)

# Declared independently of the application module, which keeps its own list.
ORDER_VARIANTS = (
    "correct",
    "correct_b",
    "unsafe",
    "racy_idempotency",
    "non_atomic",
    "lost_update",
    "payload_unchecked",
    "missing_outbox",
)
PositiveVersion = Annotated[int, Field(ge=1)]
Nonempty = Annotated[str, Field(min_length=1, max_length=1024)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AdapterError(RuntimeError):
    def __init__(self, hook, code, message):
        self.hook, self.code = hook, code
        super().__init__(message)

    def diagnostic(self):
        return {
            "severity": "error",
            "code": self.code,
            "hook": self.hook,
            "message": str(self),
        }


class FixtureIdentity(StrictModel):
    version: PositiveVersion
    hash: Hash


class Description(StrictModel):
    boundary_version: PositiveVersion
    adapter_id: Identifier
    profile: Identifier
    fixture: Identifier
    fixture_version: PositiveVersion
    fixture_hash: Hash
    operations: list[Identifier]
    faults: list[Identifier]
    invariants: dict[str, PositiveVersion]
    variants: list[Identifier]
    concurrency: Annotated[int, Field(ge=1)]
    authority: Literal["postgresql"]
    quiescence: Literal["graceful_process_exit_and_database_sessions"]
    external_registration: bool
    generators: dict[str, PositiveVersion] = Field(default_factory=dict)
    # Optional selectable fixtures besides the default `fixture`. Adapters that declare
    # any receive the selected name as `prepare(..., fixture=name)`.
    fixtures: dict[Identifier, FixtureIdentity] = Field(default_factory=dict)
    readiness_checks: list[Identifier] = Field(
        default_factory=lambda: [
            "orders_health",
            "postgres_ready",
            "worker_handshake",
            "observer_snapshot",
            "proxy_ready",
        ]
    )


class Failure(StrictModel):
    code: Nonempty
    expected: JsonValue
    observed: JsonValue
    evidence: Annotated[list[Nonempty], Field(min_length=1, max_length=100)]


class Finding(StrictModel):
    invariant_id: Nonempty
    version: PositiveVersion
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    logical_id: Identifier
    failures: Annotated[list[Failure], Field(max_length=100)]
    evidence: Annotated[list[Nonempty], Field(min_length=1, max_length=100)]
    reason: Nonempty | None = None
    signatures: Annotated[list[Hash], Field(max_length=100)] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_status(self):
        if (self.status == "FAIL") != bool(self.failures):
            raise ValueError("FAIL requires failures; PASS and UNKNOWN cannot contain failures")
        if len(self.signatures) != len(self.failures):
            raise ValueError("each failure requires one signature")
        if self.status == "UNKNOWN" and self.reason is None:
            raise ValueError("UNKNOWN requires a reason")
        if self.status != "UNKNOWN" and self.reason is not None:
            raise ValueError("only UNKNOWN may have a reason")
        return self


class Snapshot(StrictModel):
    snapshot_schema_version: Annotated[int, Field(ge=1, le=1)]
    snapshot_id: Nonempty
    role: Literal["baseline", "final"]
    authority: Literal["postgresql"]
    complete: bool
    quiescent: bool
    records: dict[str, list[dict[str, JsonValue]]]
    row_counts: dict[str, Annotated[int, Field(ge=0)]]
    content_hash: Hash
    adapter_profile: Identifier
    fixture: Identifier
    isolation: Literal["repeatable_read_read_only"]
    started_ns: Annotated[int, Field(ge=0)]
    completed_ns: Annotated[int, Field(ge=0)]
    transaction: Nonempty
    application_sessions: Annotated[int, Field(ge=0)]


class Quiescence(StrictModel):
    state: Literal["ready", "pending", "unknown"]
    evidence: Annotated[list[Nonempty], Field(min_length=1, max_length=100)]


def detached_json(value):
    """Bound returned evidence and prevent later mutation through shared containers."""
    raw = canonical_bytes(value)
    if len(raw) > 1048576:
        raise ValueError("adapter result exceeds 1 MiB")
    return json.loads(raw)


def source_identity():
    """Fingerprint this bundled Python package, including its verifier dependencies.

    Source bytes are used as installed (including line endings). This is provenance,
    not authentication, dependency attestation, or enforcement of image equivalence.
    """
    root = Path(__file__).parent
    sources = {}
    total = 0
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink() or len(sources) >= 128:
            raise ValueError("unsafe or oversized adapter source inventory")
        with path.open("rb") as stream:
            raw = stream.read(1048577)
        total += len(raw)
        if len(raw) > 1048576 or total > 4194304:
            raise ValueError("adapter source inventory exceeds size limit")
        sources[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
    if not {"adapters.py", "models.py", "verifier.py"} <= sources.keys():
        raise ValueError("bundled adapter source is unavailable")
    return {
        "package": "faultline",
        "package_version": __version__,
        "source_hash_algorithm": "sha256-canonical-source-inventory-v1",
        "content_hash": digest(sources),
        "sources": sources,
    }


class OrdersAdapter:
    """Domain hooks for the bundled profile. Verification receives no environment or variant."""

    def describe(self):
        return {
            "boundary_version": 1,
            "adapter_id": "bundled_orders",
            "profile": "orders_v1",
            "fixture": "standard_wallet",
            "fixture_version": 1,
            "fixture_hash": digest(verifier.fixture_records("standard_wallet")),
            "fixtures": {
                "reduction_market": {
                    "version": 1,
                    "hash": digest(verifier.fixture_records("reduction_market")),
                }
            },
            "operations": ["create_order"],
            "faults": ["response_suppress", "proxy_latency", "proxy_disconnect", "process_restart"],
            "invariants": dict.fromkeys(INVARIANTS, 1),
            "variants": list(ORDER_VARIANTS),
            "concurrency": 32,
            "authority": "postgresql",
            "quiescence": "graceful_process_exit_and_database_sessions",
            "external_registration": False,
            "generators": {"orders.purchases": 1},
        }

    def validate(self, scenario):
        # Recheck even direct Python callers that used model_copy without validation.
        Scenario.model_validate(scenario.model_dump(by_alias=True))
        opening = verifier.fixture_records(scenario.fixture)
        accounts = {row["account_id"] for row in opening["accounts"]}
        products = {row["sku"] for row in opening["products"]}
        identities = {}
        for operation in scenario.all_operations:
            request = operation.request
            body = request.body.model_dump()
            if operation.expectation not in {
                "success",
                "insufficient_funds",
                "insufficient_stock",
                "invalid_quantity",
                "idempotency_conflict",
                "purchase_conflict",
            }:
                raise ValueError("unsupported order expectation")
            if operation.expectation == "invalid_quantity":
                if (
                    not isinstance(body, dict)
                    or type(body.get("quantity")) is not int
                    or body["quantity"] > 0
                ):
                    raise ValueError("invalid_quantity must supply a nonpositive integer quantity")
                Purchase.model_validate({**body, "quantity": 1})
            else:
                Purchase.model_validate(body)
            if (
                body.get("account_id", "buyer-a") not in accounts
                or body.get("sku", "sku-a") not in products
            ):
                raise ValueError("purchase references an account or product outside the fixture")
            if operation.logical_id != body["purchase_id"]:
                raise ValueError("logical_id must equal purchase_id")
            if operation.expectation == "success":
                if operation.logical_id in identities and identities[operation.logical_id] != body:
                    raise ValueError("conflicting payloads require explicit conflict expectations")
                identities[operation.logical_id] = body
            headers = {k.lower(): v for k, v in request.headers.items()}
            if (
                request.method != "POST"
                or request.path != "/orders"
                or operation.route != "orders_api"
                or set(headers) != {"idempotency-key"}
                or not 1 <= len(headers["idempotency-key"]) <= 128
            ):
                raise ValueError("order operations require POST /orders and an Idempotency-Key")

    def prepare(self, environment, variant, fixture="standard_wallet"):
        argument = json.dumps({"variant": variant, "fixture": fixture})
        result = environment.call("observer", "prepare", argument, timeout=10)
        if (
            type(result) is not dict
            or set(result) != {"prepared"}
            or result["prepared"] is not True
        ):
            raise ValueError("fixture helper did not acknowledge preparation")
        baseline = self.snapshot(environment, "baseline")
        verifier.validate_baseline(baseline)
        return baseline

    def readiness(self, environment):
        environment.wait_application_ready()

    def quiesce(self, environment, timeout_ms):
        result = environment.drain(timeout_ms)
        if type(result) is not bool:
            raise ValueError("drain must return a boolean")
        return {"state": "ready" if result else "unknown", "evidence": ["history.ndjson"]}

    def snapshot(self, environment, role):
        snapshot = environment.snapshot(role)
        verifier.validate_snapshot(snapshot)
        if snapshot.get("role") != role or snapshot.get("snapshot_id") != role:
            raise ValueError("snapshot role or identity does not match the requested observation")
        return snapshot

    def verify(self, scenario, baseline, final, attempts, *, workload_complete):
        return verifier.verify(
            scenario, baseline, final, attempts, workload_complete=workload_complete
        )


class CheckedAdapter:
    """Reject broken hook contracts before the engine treats their output as evidence."""

    def __init__(self, implementation):
        self.implementation = implementation
        self.description = self._call(
            "describe", lambda value: Description.model_validate(detached_json(value))
        )
        if self.description.fixture in self.description.fixtures:
            raise AdapterError("describe", "adapter_contract_error", "Duplicate fixture name")
        d = self.description
        # Selected by validate(); until then only the default fixture is meaningful.
        self.fixture = (d.fixture, d.fixture_version, d.fixture_hash)
        if self.description.boundary_version != 1:
            raise AdapterError("describe", "adapter_contract_error", "Unsupported adapter boundary")
        for hook in ("validate", "prepare", "readiness", "quiesce", "snapshot", "verify"):
            if not callable(getattr(implementation, hook, None)):
                raise AdapterError(hook, "adapter_contract_error", "Adapter is missing a hook")

    def _call(self, hook, check, *args, **kwargs):
        try:
            result = getattr(self.implementation, hook)(*args, **kwargs)
        except Exception as exc:
            # Do not echo arbitrary hook exceptions: they may contain credentials or payloads.
            raise AdapterError(hook, "adapter_hook_error", f"Adapter {hook} hook failed") from exc
        try:
            return check(result)
        except Exception as exc:
            raise AdapterError(
                hook, "adapter_contract_error", f"Adapter {hook} returned an invalid result"
            ) from exc
        # DeadlineExceeded and KeyboardInterrupt intentionally bypass these Exception handlers.

    def validate(self, scenario, variant=None):
        descriptor = self.description
        if scenario.generators:
            generated, identities = [], []
            for generator in scenario.generators:
                if generator.kind not in descriptor.generators:
                    raise ValueError("Scenario requires an unregistered generator")
                version = descriptor.generators[generator.kind]
                if generator.kind == "orders.purchases" and version == 1:
                    operations = generator.expand(scenario.seed)
                    algorithm = "sha256-counter-v1"
                else:

                    def expansion(value, version=version, count=generator.count):
                        value = detached_json(value)
                        if (
                            not isinstance(value, dict)
                            or set(value) != {"operations", "version", "algorithm"}
                            or type(value["version"]) is not int
                            or value["version"] != version
                            or not isinstance(value["algorithm"], str)
                            or not 1 <= len(value["algorithm"]) <= 128
                            or not isinstance(value["operations"], list)
                            or len(value["operations"]) != count
                        ):
                            raise ValueError("invalid registered generator expansion")
                        return [Operation.model_validate(op) for op in value["operations"]], value[
                            "algorithm"
                        ]

                    operations, algorithm = self._call(
                        "expand", expansion, generator.model_dump(), scenario.seed
                    )
                generated.extend(operations)
                identities.append({**generator.model_dump(), "version": version, "prng": algorithm})
            scenario.materialize_generators(generated, identities)
        if scenario.fixture == descriptor.fixture:
            selected = (descriptor.fixture, descriptor.fixture_version, descriptor.fixture_hash)
        elif scenario.fixture in descriptor.fixtures:
            identity = descriptor.fixtures[scenario.fixture]
            selected = (scenario.fixture, identity.version, identity.hash)
        else:
            raise ValueError("Scenario requires an unregistered fixture")
        if (
            scenario.adapter_profile != descriptor.profile
            or scenario.concurrency > descriptor.concurrency
            or any(op.kind not in descriptor.operations for op in scenario.all_operations)
            or any(f.kind not in descriptor.faults for f in scenario.faults)
            or any(g.kind not in descriptor.generators for g in scenario.generators)
            or set(scenario.invariants) != set(descriptor.invariants)
            or (variant is not None and variant not in descriptor.variants)
        ):
            raise ValueError("Scenario or variant requires unsupported adapter capabilities")
        self._call("validate", self._nothing, scenario)
        self.fixture = selected
        scenario.bind_adapter_plan(
            {
                "adapter_id": descriptor.adapter_id,
                "boundary_version": descriptor.boundary_version,
                "profile": descriptor.profile,
                "fixture": selected[0],
                "fixture_version": selected[1],
                "fixture_hash": selected[2],
                "invariants": descriptor.invariants,
                "generators": descriptor.generators,
            }
        )

    @staticmethod
    def _nothing(value):
        if value is not None:
            raise ValueError("hook must return None on success")

    def prepare(self, environment, variant):
        name = self.fixture[0]
        # The default fixture keeps the original two-argument hook for existing adapters.
        extra = {} if name == self.description.fixture else {"fixture": name}
        return self._call(
            "prepare",
            lambda value: self._snapshot(value, "baseline"),
            environment,
            variant,
            **extra,
        )

    def readiness(self, environment):
        self._call("readiness", self._nothing, environment)

    def quiesce(self, environment, timeout_ms):
        return self._call(
            "quiesce",
            lambda value: Quiescence.model_validate(detached_json(value)).model_dump(),
            environment,
            timeout_ms,
        )

    def snapshot(self, environment, role):
        return self._call("snapshot", lambda value: self._snapshot(value, role), environment, role)

    def _snapshot(self, value, role):
        value = detached_json(value)
        snapshot = Snapshot.model_validate(value)
        if snapshot.role != role or snapshot.snapshot_id != role:
            raise ValueError("snapshot role or identity does not match the observation")
        if snapshot.content_hash != digest(snapshot.records):
            raise ValueError("snapshot content hash mismatch")
        if snapshot.row_counts != {key: len(rows) for key, rows in snapshot.records.items()}:
            raise ValueError("snapshot row counts do not match records")
        if role == "baseline" and (not snapshot.complete or not snapshot.quiescent):
            raise ValueError("baseline must be complete and quiescent")
        if role == "baseline" and snapshot.content_hash != self.fixture[2]:
            raise ValueError("baseline differs from the planned fixture digest")
        if snapshot.adapter_profile != self.description.profile:
            raise ValueError("snapshot profile mismatch")
        if snapshot.fixture != self.fixture[0]:
            raise ValueError("snapshot fixture mismatch")
        if snapshot.quiescent != (snapshot.application_sessions == 0):
            raise ValueError("snapshot quiescence contradicts database session evidence")
        return value

    def verify(self, scenario, baseline, final, attempts, *, workload_complete):
        def findings(value):
            value = detached_json(value)
            expected = {
                (name, op.logical_id)
                for name in scenario.invariants
                for op in scenario.all_operations
            }
            if type(value) is not list or len(value) != len(expected):
                raise ValueError("verifier must return exactly one result per required invariant")
            checked = [Finding.model_validate(item) for item in value]
            if {(item.invariant_id, item.logical_id) for item in checked} != expected:
                raise ValueError("missing, duplicate, or unexpected invariant results")
            for item in checked:
                if item.version != self.description.invariants[item.invariant_id]:
                    raise ValueError("unsupported invariant version")
            return [item.model_dump(exclude_none=True) for item in checked]

        return self._call(
            "verify",
            findings,
            scenario,
            baseline,
            final,
            attempts,
            workload_complete=workload_complete,
        )


def bundled_adapter():
    """Reserved built-in factory; installed selection lives in adapter_loader."""
    return CheckedAdapter(OrdersAdapter())
