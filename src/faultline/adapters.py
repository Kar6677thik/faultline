"""Checked order-profile bridge, narrower than the full public v1 adapter contract.

The bridge deliberately describes the existing synchronous order profile, not the
full public v1 API. Docker ownership and deadlines remain the engine's responsibility.
"""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from faultline import __version__, verifier
from faultline.models import INVARIANTS, Identifier, Scenario, StrictModel, canonical_bytes, digest

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


class Description(StrictModel):
    boundary_version: PositiveVersion
    adapter_id: Identifier
    profile: Identifier
    fixture: Identifier
    fixture_version: PositiveVersion
    operations: list[Identifier]
    faults: list[Identifier]
    invariants: dict[str, PositiveVersion]
    variants: list[Identifier]
    concurrency: Annotated[int, Field(ge=1)]
    authority: Literal["postgresql"]
    quiescence: Literal["graceful_process_exit_and_database_sessions"]
    external_registration: bool


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
            "operations": ["create_order"],
            "faults": ["response_suppress"],
            "invariants": dict.fromkeys(INVARIANTS, 1),
            "variants": ["unsafe", "correct"],
            "concurrency": 1,
            "authority": "postgresql",
            "quiescence": "graceful_process_exit_and_database_sessions",
            "external_registration": False,
        }

    def validate(self, scenario):
        # Recheck even direct Python callers that used model_copy without validation.
        Scenario.model_validate(scenario.model_dump(by_alias=True))

    def prepare(self, environment, variant):
        result = environment.call("observer", "prepare", variant, timeout=10)
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
        return environment.drain(timeout_ms)

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
        if (
            scenario.adapter_profile != descriptor.profile
            or scenario.fixture != descriptor.fixture
            or scenario.concurrency > descriptor.concurrency
            or any(op.kind not in descriptor.operations for op in scenario.operations)
            or any(f.kind not in descriptor.faults for f in scenario.faults)
            or set(scenario.invariants) != set(descriptor.invariants)
            or (variant is not None and variant not in descriptor.variants)
        ):
            raise ValueError("Scenario or variant requires unsupported adapter capabilities")
        self._call("validate", self._nothing, scenario)

    @staticmethod
    def _nothing(value):
        if value is not None:
            raise ValueError("hook must return None on success")

    def prepare(self, environment, variant):
        return self._call(
            "prepare", lambda value: self._snapshot(value, "baseline"), environment, variant
        )

    def readiness(self, environment):
        self._call("readiness", self._nothing, environment)

    def quiesce(self, environment, timeout_ms):
        def boolean(value):
            if type(value) is not bool:
                raise ValueError("quiesce must report a boolean graceful-exit result")
            return value

        return self._call("quiesce", boolean, environment, timeout_ms)

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
        if snapshot.adapter_profile != self.description.profile:
            raise ValueError("snapshot profile mismatch")
        if snapshot.fixture != self.description.fixture:
            raise ValueError("snapshot fixture mismatch")
        if snapshot.quiescent != (snapshot.application_sessions == 0):
            raise ValueError("snapshot quiescence contradicts database session evidence")
        return value

    def verify(self, scenario, baseline, final, attempts, *, workload_complete):
        def findings(value):
            value = detached_json(value)
            if type(value) is not list or len(value) != len(scenario.invariants):
                raise ValueError("verifier must return exactly one result per required invariant")
            checked = [Finding.model_validate(item) for item in value]
            if {item.invariant_id for item in checked} != set(scenario.invariants):
                raise ValueError("missing, duplicate, or unexpected invariant results")
            for item in checked:
                if item.version != self.description.invariants[item.invariant_id]:
                    raise ValueError("unsupported invariant version")
                if item.logical_id != scenario.operations[0].logical_id:
                    raise ValueError("finding does not match the requested logical operation")
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
