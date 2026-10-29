"""Run a defect/correct corpus through the public CLI and count every outcome.

    python -m faultline.calibration examples/orders/corpus.json --trials 20 --output DIR

Each trial is a separate `faultline run` in a fresh environment, so an ERROR in one
trial never prevents the others from starting. A defect counts as detected only when a
completed FAIL contains its declared target violation. Correct variants must PASS every
trial. These are observations of a small selected corpus, not a general detection rate.
"""

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from faultline.artifacts import atomic_json, utc_now
from faultline.groups import failure_signatures


def load_corpus(path: Path) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    if corpus.get("corpus_schema_version") != 1:
        raise ValueError("unsupported corpus version")
    for defect in corpus.get("defects", []):
        if set(defect["target"]) != {"invariant_id", "code", "logical_id"}:
            raise ValueError("defect target must name invariant_id, code, and logical_id")
    return corpus


def target_found(report: dict, target: dict) -> bool:
    return (
        report["verdict"] == "FAIL"
        and report["lifecycle"] == "completed"
        and any(
            {k: item[k] for k in ("invariant_id", "code", "logical_id")} == target
            for item in failure_signatures(report).values()
        )
    )


def one_trial(
    scenario: Path, variant: str, output: Path, timeout: int, project: Path | None = None
) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "--output",
                str(output),
                "--format",
                "json",
                *(["--project", str(project)] if project else []),
                "run",
                # Project scenarios are named relative to the project directory.
                scenario.name if project else str(scenario),
                "--variant",
                variant,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        envelope = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {"verdict": "ERROR", "lifecycle": "harness", "error": type(exc).__name__}
    trial = {
        "exit_code": envelope["exit_code"],
        "seconds": round(time.monotonic() - started, 2),
        "diagnostics": sorted({d["code"] for d in envelope["diagnostics"]}),
    }
    if not envelope["run_ids"]:
        return {**trial, "verdict": "ERROR", "lifecycle": "not_started"}
    run_id = envelope["run_ids"][0]
    report = json.loads((output / "runs" / run_id / "report.json").read_text(encoding="utf-8"))
    return {
        **trial,
        "run_id": run_id,
        "verdict": report["verdict"],
        "lifecycle": report["lifecycle"],
        "report": report,
    }


def summarize_pair(trials: list[dict], target: dict | None, strict: bool = False) -> dict:
    counts = Counter(trial["verdict"] for trial in trials)
    summary = {
        "trials": len(trials),
        "counts": {v: counts.get(v, 0) for v in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR")},
        "mean_seconds": round(sum(t.get("seconds", 0) for t in trials) / max(len(trials), 1), 1),
    }
    if target is not None:
        summary["detections"] = sum(t.get("detected", False) for t in trials)
        # AT-021: at least one conclusive detection. Strict (CI): every trial, no ERROR.
        summary["met"] = (
            summary["detections"] == len(trials) if strict else summary["detections"] >= 1
        )
    else:
        summary["met"] = counts.get("PASS", 0) == len(trials)
    return summary


def run_corpus(
    corpus_path: Path,
    output: Path,
    trials: int,
    only: str,
    timeout: int,
    strict: bool = False,
    variant: str | None = None,
) -> dict:
    corpus = load_corpus(corpus_path)
    base = corpus_path.parent
    project = base / corpus["project"] if corpus.get("project") else None
    pairs = []
    if only in {"all", "defects"}:
        pairs += [
            (d["defect"], d["variant"], d["scenario"], d["target"]) for d in corpus["defects"]
        ]
    if only in {"all", "correct"}:
        pairs += [
            ("correct", variant, scenario, None)
            for scenario in corpus["correct"]["scenarios"]
            for variant in corpus["correct"]["variants"]
        ]
    if variant is not None:
        pairs = [pair for pair in pairs if pair[1] == variant]
    record = {
        "calibration_schema_version": 1,
        "variant_filter": variant,
        "corpus": str(corpus_path),
        "profile": corpus["profile"],
        "trials_per_pair": trials,
        "strict": strict,
        "started_at": utc_now(),
        "pairs": [],
    }
    summary_path = output / "calibration.json"
    output.mkdir(parents=True, exist_ok=True)
    for kind, variant, scenario, target in pairs:
        entry = {"kind": kind, "variant": variant, "scenario": scenario, "target": target}
        record["pairs"].append(entry)
        results = []
        for index in range(trials):
            trial = one_trial(base / scenario, variant, output, timeout, project)
            report = trial.pop("report", None)
            if target is not None and report is not None:
                trial["detected"] = target_found(report, target)
            if report is not None:
                trial["violations"] = sorted(
                    f"{item['invariant_id']}/{item['code']}/{item['logical_id']}"
                    for item in failure_signatures(report).values()
                )
            results.append(trial)
            entry.update(results=results, **summarize_pair(results, target, strict))
            atomic_json(summary_path, record)
            print(
                f"{kind:30} {variant:18} {scenario:32} {index + 1:>3}/{trials} "
                f"{trial['verdict']}{' detected' if trial.get('detected') else ''}",
                file=sys.stderr,
                flush=True,
            )
    record["completed_at"] = utc_now()
    record["met"] = all(entry["met"] for entry in record["pairs"])
    atomic_json(summary_path, record)
    (output / "calibration.md").write_text(render(record), encoding="utf-8")
    return record


def render(record: dict) -> str:
    lines = [
        "# Calibration results",
        "",
        f"Corpus `{record['corpus']}`, {record['trials_per_pair']} fresh trials per pair, "
        f"{record['started_at']} to {record.get('completed_at', 'incomplete')}.",
        "",
        "| Kind | Variant | Scenario | PASS | FAIL | INCONCLUSIVE | ERROR | Detected | Met |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for pair in record["pairs"]:
        c = pair["counts"]
        detected = f"{pair['detections']}/{pair['trials']}" if pair["target"] else "n/a"
        lines.append(
            f"| {pair['kind']} | `{pair['variant']}` | `{pair['scenario']}` | {c['PASS']} | "
            f"{c['FAIL']} | {c['INCONCLUSIVE']} | {c['ERROR']} | {detected} | "
            f"{'yes' if pair['met'] else 'NO'} |"
        )
    lines += [
        "",
        "A defect is detected only by a completed FAIL containing its declared target",
        "violation. These counts describe this corpus and machine, not a general rate.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--only", choices=["all", "defects", "correct"], default="all")
    parser.add_argument("--trial-timeout", type=int, default=900)
    parser.add_argument(
        "--strict", action="store_true", help="require detection in every defect trial (CI)"
    )
    parser.add_argument("--variant", help="run only pairs for this variant")
    args = parser.parse_args(argv)
    if not 1 <= args.trials <= 100:
        parser.error("--trials must be between 1 and 100")
    record = run_corpus(
        args.corpus.resolve(),
        args.output.resolve(),
        args.trials,
        args.only,
        args.trial_timeout,
        args.strict,
        args.variant,
    )
    print(json.dumps({"met": record["met"], "summary": str(args.output / "calibration.md")}))
    return 0 if record["met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
