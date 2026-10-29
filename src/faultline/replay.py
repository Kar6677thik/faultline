"""Strict replay and explicit comparison of a saved materialized plan.

Replay re-executes the exact recorded plan in fresh resources. It only proceeds when
every recorded contract still holds; otherwise the user must ask for a comparison,
which records each difference instead of hiding it.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from faultline import __version__
from faultline.adapter_loader import resolve_adapter
from faultline.artifacts import read_bundle
from faultline.models import Scenario, digest
from faultline.projects import Project
from faultline.verifier import EvidenceError


class ReplayRejected(ValueError):
    """The source cannot be strictly replayed or validly compared; nothing was provisioned."""

    def __init__(self, message: str, differences: list[dict]):
        self.differences = differences
        super().__init__(f"{message}: " + ", ".join(d["field"] for d in differences))


@dataclass(frozen=True)
class SourceRun:
    path: Path
    manifest: dict
    report: dict
    plan: dict
    project: Project | None

    @property
    def run_id(self) -> str:
        return self.manifest["run_id"]

    @property
    def variant(self) -> str:
        return self.manifest["variant"]


def load_source(path: Path) -> SourceRun:
    """Load a hash-verified completed bundle; no adapter code or Docker is used here."""
    manifest, report = read_bundle(path)
    root = path.resolve(strict=True)
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    if digest(plan) != manifest.get("plan_hash"):
        raise EvidenceError("plan does not match the recorded plan hash")
    if not isinstance(manifest.get("platform"), dict) or "adapter" not in manifest:
        raise EvidenceError("source run stopped before its environment identity was recorded")
    project = None
    if "project" in manifest:
        saved = json.loads((root / "project.json").read_text(encoding="utf-8"))
        configuration = saved.get("configuration")
        if (
            saved.get("project_artifact_version") != 1
            or digest(configuration) != manifest["project"]["configuration_hash"]
        ):
            raise EvidenceError("saved project configuration does not match its recorded hash")
        project = Project.model_validate(configuration)
    return SourceRun(root, manifest, report, plan, project)


def differences(source: object, current: object, field: str) -> list[dict]:
    """Field-level differences between two JSON values; nested mappings are expanded."""
    if isinstance(source, dict) and isinstance(current, dict):
        found = []
        for key in sorted(source.keys() | current.keys()):
            found.extend(differences(source.get(key), current.get(key), f"{field}.{key}"))
        return found
    return [] if source == current else [{"field": field, "source": source, "current": current}]


def _adapter_view(identity: dict) -> dict:
    return {k: identity[k] for k in ("package", "package_version", "content_hash")}


def replay_scenario(source: SourceRun) -> Scenario:
    """Rebuild and check the exact plan; reject any contract drift before Docker."""
    scenario = Scenario.from_plan(source.plan, source.project)
    _adapter, identity = resolve_adapter(source.project, scenario, source.variant)
    found = differences(source.manifest["plan_hash"], digest(scenario.plan()), "plan_hash")
    found += differences(source.manifest["engine_version"], __version__, "engine_version")
    found += differences(
        _adapter_view(source.manifest["adapter"]), _adapter_view(identity), "adapter"
    )
    if found:
        raise ReplayRejected("strict replay contract mismatch", found)
    return scenario


def check_platform(source: SourceRun, platform_info: dict) -> None:
    """Images, platform tuple, project hash, and variant must all equal the source."""
    found = differences(source.manifest["platform"], platform_info, "platform")
    if found:
        raise ReplayRejected("strict replay environment mismatch", found)


def comparison_scenario(
    source: SourceRun, project: Project | None, variant: str
) -> tuple[Scenario, dict]:
    """Bind the saved plan to an explicitly chosen variant; semantics must still agree."""
    scenario = Scenario.from_plan(source.plan, project)
    _adapter, identity = resolve_adapter(project, scenario, variant)
    plan = scenario.plan()
    contract = ("profile", "fixture", "fixture_version", "fixture_hash")
    found = differences(source.plan["requests"], plan["requests"], "plan.requests")
    found += differences(
        source.plan["invariant_versions"], plan["invariant_versions"], "plan.invariant_versions"
    )
    found += differences(
        {k: source.plan["adapter_contract"].get(k) for k in contract},
        {k: plan["adapter_contract"].get(k) for k in contract},
        "plan.adapter_contract",
    )
    if found:
        # Different business inputs, invariant semantics, or fixture: no before/after claim.
        raise ReplayRejected("invalid comparison", found)
    return scenario, identity


def comparison_differences(
    source: SourceRun, scenario: Scenario, identity: dict, platform_info: dict, variant: str
) -> list[dict]:
    """Every recorded difference between the source run and the comparison environment."""
    found = differences(source.variant, variant, "variant")
    found += differences(source.manifest["engine_version"], __version__, "engine_version")
    found += differences(
        _adapter_view(source.manifest["adapter"]), _adapter_view(identity), "adapter"
    )
    found += differences(source.manifest["plan_hash"], digest(scenario.plan()), "plan_hash")
    found += differences(source.manifest["platform"], platform_info, "platform")
    return found
