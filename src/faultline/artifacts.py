"""Durable evidence with bounded writes and integrity-checked offline reading."""

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from faultline import __version__
from faultline.models import canonical_bytes, digest
from faultline.verifier import EvidenceError


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(canonical_bytes(data) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Bundle:
    def __init__(self, output: Path, scenario, variant: str):
        self.run_id = str(uuid4())
        self.path = output / "runs" / self.run_id
        self.path.mkdir(parents=True, exist_ok=False)
        (self.path / "snapshots").mkdir()
        self.budget = scenario.limits.artifact_bytes
        self.sequence = 0
        self.manifest = {
            "bundle_schema_version": 1,
            "run_id": self.run_id,
            "engine_version": __version__,
            "implementation_profile": "milestone1",
            # Recovery must acquire the original execution lock, not a copied bundle's lock.
            "execution_root": str(output.resolve()),
            "created_at": utc_now(),
            "scenario_id": scenario.id,
            "variant": variant,
            "stage": "created",
            "ownership_nonce": str(uuid4()),
            "resources": [],
            "artifacts": {},
            "plan_hash": digest(scenario.plan()),
        }
        self.write("plan.json", scenario.plan())
        self.save_manifest()

    def _check_budget(self, addition: int):
        used = sum(path.stat().st_size for path in self.path.rglob("*") if path.is_file())
        if used + addition > self.budget:
            raise OSError("artifact budget exhausted")

    def write(self, name: str, value: dict):
        self._check_budget(len(canonical_bytes(value)))
        atomic_json(self.path / name, value)

    def save_manifest(self):
        atomic_json(self.path / "manifest.json", self.manifest)

    def event(self, kind: str, payload: dict, **refs) -> str:
        event_id = str(uuid4())
        now = time.monotonic_ns()
        event = {
            "event_schema_version": 1,
            "run_id": self.run_id,
            "event_id": event_id,
            "sequence": self.sequence + 1,
            "source": "coordinator",
            "source_sequence": self.sequence + 1,
            "source_monotonic_ns": now,
            "received_monotonic_ns": now,
            "wall_time": utc_now(),
            "type": kind,
            "payload": payload,
            **refs,
        }
        raw = canonical_bytes(event) + b"\n"
        self._check_budget(len(raw))
        with (self.path / "history.ndjson").open("ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.sequence += 1
        return event_id

    def stage(self, stage: str):
        self.manifest["stage"] = stage
        self.save_manifest()
        self.event("lifecycle", {"stage": stage})

    def finalize(self, report: dict):
        report["report_schema_version"] = 1
        report["run_id"] = self.run_id
        report["last_sequence"] = self.sequence
        report["evidence_hashes"] = {}
        for path in self.path.rglob("*"):
            if path.is_file() and path.name != "manifest.json":
                report["evidence_hashes"][path.relative_to(self.path).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        # Final diagnostics are allowed a small reserved allowance after a capture budget error.
        atomic_json(self.path / "report.json", report)
        text = render_markdown(report, self.manifest)
        (self.path / "report.md").write_text(text, encoding="utf-8")
        self.manifest["stage"] = "completed"
        self.manifest["completed_at"] = utc_now()
        self.manifest["verdict"] = report["verdict"]
        for path in self.path.rglob("*"):
            if path.is_file() and path.name != "manifest.json":
                self.manifest["artifacts"][path.relative_to(self.path).as_posix()] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
        self.save_manifest()


def render_markdown(report: dict, manifest: dict) -> str:
    lines = [
        f"# Faultline: {report['verdict']}",
        "",
        f"Run: `{report['run_id']}`",
        f"Scenario: `{manifest['scenario_id']}`",
        f"Variant: `{manifest['variant']}`",
        "",
        "Scope: one purchase, controlled client response suppression, PostgreSQL authority.",
        "This is not TCP packet loss or proof of correctness under all schedules.",
        "",
        f"Required fault coverage: {report['coverage']['required_applied']}/"
        f"{report['coverage']['required_total']}",
        f"Cleanup: {'complete' if report['cleanup']['complete'] else 'INCOMPLETE'}",
        "",
    ]
    for result in report["invariants"]:
        lines.extend([f"## {result['invariant_id']}: {result['status']}", ""])
        for failure in result["failures"]:
            lines.extend(
                [
                    f"- {failure['code']}",
                    f"- Expected: `{json.dumps(failure['expected'])}`",
                    f"- Observed: `{json.dumps(failure['observed'])}`",
                    f"- Evidence: {', '.join(failure['evidence'])}",
                    "",
                ]
            )
        if result.get("reason"):
            lines.extend([result["reason"], ""])
    if report["diagnostics"]:
        lines.extend(["## Diagnostics", ""])
        lines.extend(f"- {d['code']}: {d['message']}" for d in report["diagnostics"])
    return "\n".join(lines) + "\n"


def read_bundle(path: Path) -> tuple[dict, dict]:
    root = path.resolve(strict=True)
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 1048576:
        raise EvidenceError("unsafe manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("bundle_schema_version") != 1 or manifest.get("stage") != "completed":
        raise EvidenceError("unsupported or incomplete bundle")
    inventory = manifest.get("artifacts", {})
    required = {"plan.json", "history.ndjson", "report.json", "report.md"}
    if not required.issubset(inventory):
        raise EvidenceError("missing required artifacts")
    total = 0
    for name, expected in inventory.items():
        candidate = root / name
        if "\\" in name or any(part in {"..", ""} for part in name.split("/")):
            raise EvidenceError("unsafe artifact path")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or candidate.is_symlink():
            raise EvidenceError("artifact escapes bundle")
        total += resolved.stat().st_size
        if total > 53000000:
            raise EvidenceError("bundle too large")
        raw = resolved.read_bytes()
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise EvidenceError(f"corrupt artifact: {name}")
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    if report.get("report_schema_version") != 1 or report.get("run_id") != manifest["run_id"]:
        raise EvidenceError("invalid report identity or version")
    events = (root / "history.ndjson").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(events, 1):
        event = json.loads(line)
        if event.get("sequence") != index or event.get("run_id") != manifest["run_id"]:
            raise EvidenceError("invalid history sequence or identity")
    if len(events) != report["last_sequence"]:
        raise EvidenceError("history incomplete")
    return manifest, report
