"""Honest accounting for repeated, replayed, and compared runs.

A group never turns a run into a reproduction by itself. It counts what each
member's own report says, and keeps every member visible in the totals.
"""

from pathlib import Path
from uuid import uuid4

from faultline.artifacts import atomic_json, utc_now

VERDICTS = ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")
KINDS = {"repeated_run", "replay", "comparison"}


def failure_signatures(report: dict) -> dict[str, dict]:
    """Map each stable violation signature to the readable fields it summarizes."""
    catalog = {}
    for finding in report.get("invariants", []):
        for failure, signature in zip(
            finding.get("failures", []), finding.get("signatures", []), strict=False
        ):
            catalog.setdefault(
                signature,
                {
                    "invariant_id": finding["invariant_id"],
                    "version": finding["version"],
                    "code": failure["code"],
                    "logical_id": finding.get("logical_id"),
                },
            )
    return catalog


def matches(report: dict, signature: str | None) -> bool:
    """A reproduction is a completed FAIL carrying the target; ERROR never qualifies."""
    return (
        signature is not None
        and report["verdict"] == "FAIL"
        and report["lifecycle"] == "completed"
        and signature in failure_signatures(report)
    )


def fraction(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "ratio": f"{numerator}/{denominator}" if denominator else "not_available",
    }


def summarize(reports: list[dict], scheduled: int, signature: str | None) -> dict:
    started = len(reports)
    counts = {state: sum(r["verdict"] == state for r in reports) for state in VERDICTS}
    interrupted = sum(r["lifecycle"] == "interrupted" for r in reports)
    matching = sum(matches(r, signature) for r in reports)
    # Conclusive means a completed PASS or FAIL; verdict precedence already excludes errors.
    conclusive = sum(
        r["lifecycle"] == "completed" and r["verdict"] in {"PASS", "FAIL"} for r in reports
    )
    summary = {
        "scheduled": scheduled,
        "started": started,
        "completed": started - interrupted,
        "interrupted": interrupted,
        "unstarted": scheduled - started,
        "counts": counts,
        "target_signature": signature,
        "matching_failures": matching if signature else None,
        "other_failures": counts["FAIL"] - matching if signature else None,
        "cleanup_complete": all(r["cleanup"]["complete"] for r in reports),
    }
    if signature:
        summary["reproduction"] = {
            "per_started": fraction(matching, started),
            "per_conclusive": fraction(matching, conclusive),
        }
    overall = next((s for s in ("ERROR", "FAIL", "INCONCLUSIVE", "PASS") if counts[s]), None)
    # Mixed outcomes never become PASS. With no started run there is no verdict yet.
    summary["verdict"] = overall
    return summary


class Group:
    """Persist group state after every trial so an interruption keeps its counts."""

    def __init__(
        self,
        output: Path,
        kind: str,
        *,
        scheduled: int,
        signature: str | None = None,
        details: dict | None = None,
    ):
        if kind not in KINDS:
            raise ValueError("unsupported group kind")
        self.id = str(uuid4())
        self.path = output / "groups" / f"{self.id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduled, self.signature = scheduled, signature
        self.reports, self.trials = [], []
        self.record = {
            "group_schema_version": 1,
            "group_id": self.id,
            "kind": kind,
            "created_at": utc_now(),
            "status": "running",
            **(details or {}),
        }
        self.save()

    def add(self, report: dict):
        self.reports.append(report)
        self.trials.append(
            {
                "run_id": report["run_id"],
                "verdict": report["verdict"],
                "lifecycle": report["lifecycle"],
                "matches_target": matches(report, self.signature),
                "signatures": sorted(failure_signatures(report)),
            }
        )
        self.save()

    def close(self, status: str) -> dict:
        self.record["status"] = status
        self.record["completed_at"] = utc_now()
        self.save()
        return self.summary()

    def summary(self) -> dict:
        return summarize(self.reports, self.scheduled, self.signature)

    def save(self):
        atomic_json(
            self.path,
            {
                **self.record,
                "run_ids": [trial["run_id"] for trial in self.trials],
                "trials": self.trials,
                **self.summary(),
            },
        )
