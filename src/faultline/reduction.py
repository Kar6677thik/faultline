"""Dependency-aware reduction of a failing plan toward a smaller reproduction.

The reducer deletes whole logical operations, fault rules, and retry suffixes. It never
edits request payloads, timings, or the fixture. A change is kept only when fresh trials
reproduce the *same* target signature, and the final result is called verified only
after a separate five-run validation. "Smaller", never "minimal".
"""

import json
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from faultline.artifacts import atomic_json, utc_now
from faultline.groups import failure_signatures, matches
from faultline.models import canonical_bytes, digest

BASELINE_RUNS = 5
BASELINE_REQUIRED = 4
CANDIDATE_RUNS = 3
VALIDATION_RUNS = 5
VALIDATION_REQUIRED = 4
DEFAULT_MAX_RUNS = 50
DEFAULT_MAX_DURATION_MS = 600000
MAX_RUNS = 500
MAX_DURATION_MS = 3600000
# Spec 09: repeat/reduction jobs default to a 1 GiB total artifact budget.
GROUP_ARTIFACT_BYTES = 1073741824
EXIT = {
    "reduction_verified": 0,
    "no_smaller_reproduction": 0,
    "baseline_unstable": 3,
    "reduction_unverified": 3,
    "insufficient_budget": 3,
}


class BudgetExhausted(Exception):
    """A trial cannot start inside the remaining run, time, or artifact budget."""


def objective(raw: dict) -> list[int]:
    """Lexicographic objective: operations, then faults, then potential attempts."""
    operations = raw["operations"]
    return [
        len(operations),
        len(raw["faults"]),
        sum(op.get("retry", {}).get("max_attempts", 1) for op in operations),
    ]


def remove(raw: dict, operations=(), faults=()) -> dict:
    """Delete units and everything that depends on them; never leave a dangling reference."""
    candidate = deepcopy(raw)
    dead_ops, dead_faults = set(operations), set(faults)
    changed = True
    while changed:
        changed = False
        for fault in candidate["faults"]:
            trigger = fault["trigger"]
            if fault["id"] not in dead_faults and trigger.get("operation_id") in dead_ops:
                dead_faults.add(fault["id"])
                changed = True
        for op in candidate["operations"]:
            if op["id"] in dead_ops:
                continue
            if set(op.get("after", [])) & dead_ops or set(op.get("after_faults", [])) & dead_faults:
                # v1 never rewrites a gate into an "equivalent" ungated plan.
                dead_ops.add(op["id"])
                changed = True
    candidate["operations"] = [op for op in candidate["operations"] if op["id"] not in dead_ops]
    candidate["faults"] = [f for f in candidate["faults"] if f["id"] not in dead_faults]
    barriers = []
    for barrier in candidate.get("barriers", []):
        members = [m for m in barrier["members"] if m not in dead_ops]
        if members:
            barriers.append({**barrier, "members": members})
    candidate["barriers"] = barriers
    return candidate


def trim_retry(raw: dict, operation_id: str) -> dict | None:
    """Remove the last potential attempt of one operation, with dependent faults."""
    candidate = deepcopy(raw)
    op = next(op for op in candidate["operations"] if op["id"] == operation_id)
    retry = op.get("retry", {"max_attempts": 1, "on": [], "backoff_ms": []})
    if retry["max_attempts"] <= 1:
        return None
    attempts = retry["max_attempts"] - 1
    op["retry"] = {**retry, "max_attempts": attempts, "backoff_ms": retry["backoff_ms"][:-1]}
    stale = [
        f["id"]
        for f in candidate["faults"]
        if f["trigger"].get("operation_id") == operation_id and f["trigger"]["attempt"] > attempts
    ]
    return remove(candidate, faults=stale)


def chunks(items: list, count: int) -> list[list]:
    size, extra = divmod(len(items), count)
    result, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        if start < end:
            result.append(items[start:end])
        start = end
    return result


class Reducer:
    """Search with a fixed environment, a fixed target, and hard budgets.

    `validate(raw)` returns a runnable scenario or raises; it costs no trial budget.
    `trial(scenario, purpose)` runs one fresh environment and returns (path, report).
    """

    def __init__(
        self,
        output: Path,
        source,
        signature: str,
        *,
        validate: Callable,
        trial: Callable,
        max_runs: int = DEFAULT_MAX_RUNS,
        max_duration_ms: int = DEFAULT_MAX_DURATION_MS,
        artifact_bytes: int = 52428800,
        clock: Callable[[], float] = time.monotonic,
        environment: dict | None = None,
    ):
        if not 1 <= max_runs <= MAX_RUNS or not 1 <= max_duration_ms <= MAX_DURATION_MS:
            raise ValueError("reduction budget exceeds the supported maximum")
        catalog = failure_signatures(source.report)
        if source.report["verdict"] != "FAIL" or signature not in catalog:
            raise ValueError("reduction requires a completed FAIL source with the target")
        self.source, self.signature = source, signature
        self.validate, self.trial, self.clock = validate, trial, clock
        self.max_runs, self.max_duration_ms = max_runs, max_duration_ms
        self.artifact_bytes = artifact_bytes
        self.started = clock()
        self.runs_used, self.bytes_used, self.trial_seconds = 0, 0, []
        self.id = str(uuid4())
        self.root = output / "groups" / self.id
        (self.root / "candidates").mkdir(parents=True)
        self.path = output / "groups" / f"{self.id}.json"
        self.original = source.plan["scenario"]
        self.best_raw = self.original
        self.record = {
            "group_schema_version": 1,
            "group_id": self.id,
            "kind": "reduction",
            "status": "running",
            "created_at": utc_now(),
            "source_run_id": source.run_id,
            "source_plan_hash": source.manifest["plan_hash"],
            "variant": source.variant,
            "target_signature": signature,
            "target": catalog[signature],
            "environment_hash": digest(environment or source.manifest["platform"]),
            "budget": {
                "max_runs": max_runs,
                "max_duration_ms": max_duration_ms,
                "max_artifact_bytes": GROUP_ARTIFACT_BYTES,
            },
            "original_objective": objective(self.original),
            "baseline": None,
            "candidates": [],
            "best": None,
            "validation": None,
            "outcome": None,
            "termination_reason": None,
            "run_ids": [],
        }
        self.save()

    # -- bookkeeping -------------------------------------------------------------
    def elapsed_ms(self) -> int:
        return round((self.clock() - self.started) * 1000)

    def save(self):
        self.record["budget"].update(
            runs_used=self.runs_used,
            elapsed_ms=self.elapsed_ms(),
            artifact_bytes_used=self.bytes_used,
        )
        atomic_json(self.path, self.record)

    def estimate_seconds(self) -> float:
        return max(self.trial_seconds) if self.trial_seconds else 0.0

    def can_start(self, reserve_runs: int = 0) -> bool:
        """Leave room for `reserve_runs` later trials, in count, time, and disk."""
        needed = 1 + reserve_runs
        seconds = self.estimate_seconds() * needed
        return (
            self.runs_used + needed <= self.max_runs
            and self.elapsed_ms() + seconds * 1000 <= self.max_duration_ms
            and self.bytes_used + self.artifact_bytes * needed <= GROUP_ARTIFACT_BYTES
        )

    def run_trial(self, scenario, purpose: str, reserve_runs: int = 0) -> dict:
        if not self.can_start(reserve_runs):
            raise BudgetExhausted
        self.runs_used += 1
        begun = self.clock()
        path, report = self.trial(scenario, purpose)
        self.trial_seconds.append(self.clock() - begun)
        self.bytes_used += sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
        self.record["run_ids"].append(report["run_id"])
        trial = {
            "run_id": report["run_id"],
            "purpose": purpose,
            "verdict": report["verdict"],
            "lifecycle": report["lifecycle"],
            "matches_target": matches(report, self.signature),
            "signatures": sorted(failure_signatures(report)),
        }
        self.save()
        if report["lifecycle"] == "interrupted":
            # The run already stopped cleanly; the search must not admit more work.
            raise KeyboardInterrupt
        return trial

    def series(self, scenario, purpose: str, count: int, required: int) -> dict:
        trials = []
        for _ in range(count):
            trials.append(self.run_trial(scenario, purpose))
            self.save()
        matching = sum(t["matches_target"] for t in trials)
        errors = sum(t["verdict"] == "ERROR" for t in trials)
        return {
            "trials": trials,
            "scheduled": count,
            "matching": matching,
            "errors": errors,
            "stable": matching >= required and errors == 0,
        }

    # -- search --------------------------------------------------------------------
    def attempt(self, current: dict, raw: dict, phase: str, removed: dict) -> bool:
        """Validate a candidate, then run up to three trials; keep only full agreement."""
        entry = {
            "index": len(self.record["candidates"]) + 1,
            "phase": phase,
            "removed": removed,
            "objective": objective(raw),
            "trials": [],
        }
        self.record["candidates"].append(entry)
        if objective(raw) >= objective(current):
            entry.update(valid=False, accepted=False, reason="objective_not_improved")
            self.save()
            return False
        try:
            scenario = self.validate(raw)
        except Exception as exc:
            # Invalid candidates cost planning time only, never trial budget.
            entry.update(valid=False, accepted=False, reason=f"invalid: {exc}"[:500])
            self.save()
            return False
        entry["valid"] = True
        entry["plan_hash"] = digest(scenario.plan())
        plan_file = f"candidates/{entry['index']:04d}.json"
        atomic_json(self.root / plan_file, scenario.plan())
        entry["plan_file"] = f"{self.id}/{plan_file}"
        try:
            for _ in range(CANDIDATE_RUNS):
                trial = self.run_trial(scenario, f"candidate-{entry['index']}", VALIDATION_RUNS)
                entry["trials"].append(trial)
                if not trial["matches_target"] or trial["verdict"] == "ERROR":
                    entry.update(accepted=False, reason="nonmatching_trial")
                    self.save()
                    return False
        except BudgetExhausted:
            entry.update(accepted=False, reason="budget_exhausted")
            self.save()
            raise
        entry.update(accepted=True, reason="three_matching_trials")
        self.best_raw = raw
        self.record["best"] = {
            "candidate": entry["index"],
            "objective": entry["objective"],
            "plan_hash": entry["plan_hash"],
            "plan_file": entry["plan_file"],
            "removed_operations": sorted(
                {op["id"] for op in self.original["operations"]}
                - {op["id"] for op in raw["operations"]}
            ),
            "removed_faults": sorted(
                {f["id"] for f in self.original["faults"]} - {f["id"] for f in raw["faults"]}
            ),
        }
        self.save()
        return True

    def ddmin(self, current: dict, phase: str) -> dict:
        key = "operations" if phase == "operations" else "faults"
        units = [item["id"] for item in current[key]]
        granularity = 2
        while len(units) >= (2 if phase == "operations" else 1):
            reduced = False
            for chunk in chunks(units, min(granularity, len(units))):
                raw = remove(current, **{key: chunk})
                if self.attempt(current, raw, phase, {key: chunk}):
                    current = raw
                    units = [item["id"] for item in current[key]]
                    granularity = max(granularity - 1, 2)
                    reduced = True
                    break
            if not reduced:
                if granularity >= len(units):
                    break
                granularity = min(len(units), granularity * 2)
        return current

    def retries(self, current: dict) -> dict:
        for operation_id in [op["id"] for op in current["operations"]]:
            while True:
                if operation_id not in {op["id"] for op in current["operations"]}:
                    break
                raw = trim_retry(current, operation_id)
                if raw is None or not self.attempt(
                    current, raw, "retries", {"retry_suffix_of": operation_id}
                ):
                    break
                current = raw
        return current

    def run(self) -> dict:
        try:
            outcome, reason = self._run()
        except KeyboardInterrupt:
            self.record.update(status="interrupted", termination_reason="interrupted")
            self.save()
            raise
        except Exception:
            self.record.update(status="harness_error", termination_reason="harness_error")
            self.save()
            raise
        self.record.update(
            status="completed",
            outcome=outcome,
            termination_reason=reason,
            completed_at=utc_now(),
        )
        self.save()
        return self.record

    def _run(self) -> tuple[str, str]:
        if self.max_runs < BASELINE_RUNS + VALIDATION_RUNS:
            return "insufficient_budget", "max_runs_below_baseline_and_validation"
        original = self.validate(self.original)
        try:
            baseline = self.series(original, "baseline", BASELINE_RUNS, BASELINE_REQUIRED)
        except BudgetExhausted:
            return "insufficient_budget", "budget_exhausted_during_baseline"
        self.record["baseline"] = baseline
        self.save()
        if not baseline["stable"]:
            return "baseline_unstable", "baseline_below_four_of_five_or_error"
        current, reason = self.original, "search_exhausted"
        try:
            current = self.ddmin(current, "operations")
            current = self.ddmin(current, "faults")
            current = self.retries(current)
        except BudgetExhausted:
            reason = "budget_exhausted"
            current = self.best_raw
        if self.record["best"] is None:
            # Running out of budget before any improvement is not evidence that none exists.
            if reason == "budget_exhausted":
                return "insufficient_budget", reason
            return "no_smaller_reproduction", reason
        reduced = self.validate(current)
        atomic_json(self.root / "reduced-scenario.json", json.loads(canonical_bytes(current)))
        self.record["best"]["scenario_file"] = f"{self.id}/reduced-scenario.json"
        self.record["best"]["original_objective"] = objective(self.original)
        try:
            validation = self.series(reduced, "validation", VALIDATION_RUNS, VALIDATION_REQUIRED)
        except BudgetExhausted:
            self.record["validation"] = {"completed": False}
            return "reduction_unverified", "budget_exhausted_before_validation"
        validation["completed"] = True
        self.record["validation"] = validation
        if validation["stable"]:
            return "reduction_verified", reason
        return "reduction_unverified", "validation_below_four_of_five_or_error"
