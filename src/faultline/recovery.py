"""Recover recorded resources without rewriting experiment evidence or inferring ownership."""

import json
import os
import re
import stat
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from faultline.artifacts import atomic_json, utc_now
from faultline.environment import Environment
from faultline.models import digest
from faultline.ownership import canonical_uuid, validate_intents, validate_resources

MAX_MANIFEST_BYTES = 1048576
MAX_RUNS = 1000


def plain_directory(path: Path):
    """The selected output root is resolved once; descendants must not redirect elsewhere."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Unsafe recovery directory: expected a directory without symlinks")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field in ownership manifest")
        result[key] = value
    return result


def read_record(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Unsafe ownership manifest: expected a bounded regular file")
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Ownership manifest too large")
    return json.loads(raw, object_pairs_hook=unique_object)


def read_ownership(output: Path, run_id: str) -> dict:
    canonical_uuid(run_id)
    plain_directory(output / "runs")
    root = output / "runs" / run_id
    plain_directory(root)
    manifest = read_record(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("Expected an ownership manifest object")
    if manifest.get("execution_root") != str(output.resolve()):
        raise ValueError(
            "Original execution root missing or different; automatic recovery is refused. "
            "Use the original output root; legacy bundles without this record are read-only."
        )
    if (
        type(manifest.get("bundle_schema_version")) is not int
        or manifest["bundle_schema_version"] != 1
        or manifest.get("implementation_profile") not in {"milestone1", "experiment_engine_v1"}
        or manifest.get("run_id") != run_id
    ):
        raise ValueError("Unsupported ownership manifest version, profile, or run identity")
    canonical_uuid(manifest.get("ownership_nonce"))
    project_id = manifest.get("project_id", "orders-m1")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project_id):
        raise ValueError("Invalid recorded project ownership label")
    if "project" in manifest:
        if (
            not isinstance(manifest["project"], dict)
            or manifest["project"].get("project_id") != project_id
        ):
            raise ValueError("Recorded project identity disagrees with the ownership label")
    validate_resources(manifest.get("resources"))
    validate_intents(manifest.get("creation_intents", []), run_id)
    return manifest


def recovery_manifests(output: Path, run_id: str | None) -> list[dict]:
    """Validate the entire selection before any daemon mutation; caller holds the output lock."""
    if run_id is not None:
        return [read_ownership(output, run_id)]
    root = output / "runs"
    if not root.exists() and not root.is_symlink():
        return []
    plain_directory(root)
    run_ids = []
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_RUNS:
                raise ValueError("Too many run entries for --stale; use clean --run")
            # An unknown entry is not evidence of ownership. Do not enumerate Docker by labels.
            try:
                run_ids.append(canonical_uuid(entry.name))
            except ValueError as exc:
                raise ValueError("Unrecognized entry in runs directory; use clean --run") from exc
    return [read_ownership(output, value) for value in sorted(run_ids)]


def binding_record(root, manifest):
    """Reusing the first recorded ID prevents rebinding a reused name on a later cleanup."""
    path = root / f"{manifest['run_id']}.json"
    expected = {
        "binding_schema_version": 1,
        "run_id": manifest["run_id"],
        "ownership_nonce": manifest["ownership_nonce"],
        "manifest_hash": digest(manifest),
    }
    if not path.exists() and not path.is_symlink():
        return path, {**expected, "resources": []}
    record = read_record(path)
    if (
        not isinstance(record, dict)
        or set(record) != {*expected, "resources"}
        or type(record.get("binding_schema_version")) is not int
        or any(record.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("Recovery binding does not match the original ownership manifest")
    validate_resources(record["resources"])
    validate_resources([*manifest["resources"], *record["resources"]])
    pending = {(i["kind"], i["role"]) for i in manifest.get("creation_intents", [])}
    if any((r["kind"], r["role"]) not in pending for r in record["resources"]):
        raise ValueError("Recovery binding lacks a recorded creation intent")
    return path, record


def recover(output: Path, manifests: list[dict], client) -> tuple[list[dict], list[Path]]:
    """Caller holds the same lock as execution, through validation, deletion, and receipts."""
    receipt_root = output / "cleanup"
    receipt_root.mkdir(exist_ok=True)
    plain_directory(receipt_root)
    binding_root = receipt_root / "bindings"
    binding_root.mkdir(exist_ok=True)
    plain_directory(binding_root)
    # Validate every saved binding before mutating any selected Docker resource.
    bindings = [binding_record(binding_root, manifest) for manifest in manifests]
    results, paths = [], []
    for manifest, (binding_path, binding) in zip(manifests, bindings, strict=True):
        run_id = manifest["run_id"]
        receipt = {
            "cleanup_schema_version": 1,
            "cleanup_id": str(uuid4()),
            "run_id": run_id,
            "ownership_nonce": manifest["ownership_nonce"],
            "started_at": utc_now(),
            "recorded_resources": manifest["resources"],
            "creation_intents": manifest.get("creation_intents", []),
            "binding_record": str(binding_path.relative_to(output)),
            "complete": False,
            "remaining_resources": [
                *manifest["resources"],
                *binding["resources"],
                *(
                    i
                    for i in manifest.get("creation_intents", [])
                    if (i["kind"], i["role"])
                    not in {
                        (r["kind"], r["role"])
                        for r in [*manifest["resources"], *binding["resources"]]
                    }
                ),
            ],
        }
        path = receipt_root / f"{receipt['cleanup_id']}.json"
        # Fail before deletion if we cannot write recovery evidence.
        atomic_json(path, receipt)
        working = deepcopy(manifest)
        working["resources"].extend(binding["resources"])

        def persist(record, binding=binding, binding_path=binding_path):
            updated = {**binding, "resources": [*binding["resources"], record]}
            # On any failure, do not grant this candidate mutation rights in memory.
            atomic_json(binding_path, updated)
            binding.update(updated)

        env = Environment(
            client, SimpleNamespace(manifest=working, run_id=run_id), {}, persist_binding=persist
        )
        receipt.update(env.cleanup())
        receipt["reconciled_resources"] = binding["resources"]
        receipt["completed_at"] = utc_now()
        atomic_json(path, receipt)
        results.append(receipt)
        paths.append(path)
    return results, paths
