"""Recover recorded resources without rewriting experiment evidence or inferring ownership."""

import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from faultline.artifacts import atomic_json, utc_now
from faultline.environment import Environment

MAX_MANIFEST_BYTES = 1048576
MAX_RUNS = 1000
ROLES = {
    "container": {"application", "database", "observer", "worker"},
    "network": {"client", "storage"},
}


def canonical_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical run/ownership UUID")
    return value


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


def read_ownership(output: Path, run_id: str) -> dict:
    canonical_uuid(run_id)
    plain_directory(output / "runs")
    root = output / "runs" / run_id
    plain_directory(root)
    descriptor = os.open(root / "manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Unsafe ownership manifest: expected a bounded regular file")
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Ownership manifest too large")
    manifest = json.loads(raw, object_pairs_hook=unique_object)
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
        or manifest.get("implementation_profile") != "milestone1"
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
    resources = manifest.get("resources")
    if not isinstance(resources, list) or len(resources) > 6:
        raise ValueError("Invalid milestone1 resource inventory")
    ids, roles = set(), set()
    for record in resources:
        if not isinstance(record, dict) or set(record) != {"kind", "id", "role"}:
            raise ValueError("Malformed resource ownership record")
        kind, resource_id, role = record["kind"], record["id"], record["role"]
        if not all(isinstance(value, str) for value in (kind, resource_id, role)):
            raise ValueError("Resource ownership fields must be strings")
        if kind not in ROLES or role not in ROLES[kind]:
            raise ValueError("Unsupported resource kind or role")
        if not re.fullmatch(r"[0-9a-f]{64}", resource_id):
            raise ValueError("Recovery requires full immutable Docker resource IDs")
        if resource_id in ids or (kind, role) in roles:
            raise ValueError("Duplicate resource identity or role")
        ids.add(resource_id)
        roles.add((kind, role))
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


def recover(output: Path, manifests: list[dict], client) -> tuple[list[dict], list[Path]]:
    """Caller holds the same lock as execution, through validation, deletion, and receipts."""
    receipt_root = output / "cleanup"
    receipt_root.mkdir(exist_ok=True)
    plain_directory(receipt_root)
    results, paths = [], []
    for manifest in manifests:
        run_id = manifest["run_id"]
        receipt = {
            "cleanup_schema_version": 1,
            "cleanup_id": str(uuid4()),
            "run_id": run_id,
            "ownership_nonce": manifest["ownership_nonce"],
            "started_at": utc_now(),
            "recorded_resources": manifest["resources"],
            "complete": False,
            "remaining_resources": manifest["resources"],
        }
        path = receipt_root / f"{receipt['cleanup_id']}.json"
        # Fail before deletion if we cannot write recovery evidence.
        atomic_json(path, receipt)
        env = Environment(client, SimpleNamespace(manifest=manifest, run_id=run_id), {})
        receipt.update(env.cleanup())
        receipt["completed_at"] = utc_now()
        atomic_json(path, receipt)
        results.append(receipt)
        paths.append(path)
    return results, paths
