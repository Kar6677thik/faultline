"""CLI for the intentionally constrained first experiment milestone."""

import json
import signal
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from pydantic import ValidationError

from faultline.adapter_loader import resolve_adapter
from faultline.adapters import AdapterError
from faultline.artifacts import read_bundle
from faultline.engine import run_one
from faultline.environment import client_and_preflight
from faultline.groups import Group, failure_signatures, summarize
from faultline.locking import ActiveRunError, execution_lock
from faultline.models import EXIT_CODES, Scenario, digest, load_scenario
from faultline.projects import load_project
from faultline.recovery import recover, recovery_manifests
from faultline.reduction import (
    DEFAULT_MAX_DURATION_MS,
    DEFAULT_MAX_RUNS,
    MAX_DURATION_MS,
    MAX_RUNS,
    Reducer,
)
from faultline.reduction import EXIT as REDUCTION_EXIT
from faultline.replay import (
    ReplayRejected,
    check_platform,
    comparison_differences,
    comparison_scenario,
    load_source,
    replay_scenario,
)

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Faultline: disposable correctness experiments, replay, and comparison. Linux/WSL2.",
)


@app.callback()
def options(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="Artifact root")] = Path(".faultline"),
    format: Annotated[str, typer.Option(help="text or json")] = "text",
    no_color: Annotated[bool, typer.Option()] = False,
    project: Annotated[
        Path | None, typer.Option(help="Project manifest; defaults to cwd if present")
    ] = None,
):
    if format not in {"text", "json"}:
        raise typer.BadParameter("format must be text or json")
    ctx.obj = {"output": output.resolve(), "format": format, "project": project}
    if no_color:
        ctx.color = False


def finish(
    ctx, command, code, summary, *, paths=None, run_ids=None, diagnostics=None, group_id=None
):
    envelope = {
        "cli_schema_version": 1,
        "command": command,
        "exit_code": code,
        "outcome": summary.get("verdict", "PASS" if code == 0 else "ERROR"),
        "run_ids": run_ids or [],
        "artifact_paths": [str(p) for p in paths or []],
        "summary": summary,
        "diagnostics": diagnostics or [],
    }
    if group_id:
        envelope["group_id"] = group_id
    if ctx.obj["format"] == "json":
        typer.echo(json.dumps(envelope))
    else:
        typer.echo(f"{command}: {envelope['outcome']}")
        for key, value in summary.items():
            typer.echo(f"  {key}: {value}")
        for path in paths or []:
            typer.echo(f"  Evidence: {path}")
        for diagnostic in diagnostics or []:
            typer.echo(f"  {diagnostic['code']}: {diagnostic['message']}")
    raise typer.Exit(code)


def input_error(ctx, command, exc, code=2):
    if isinstance(exc, AdapterError):
        finish(ctx, command, 4, {}, diagnostics=[exc.diagnostic()])
    message = str(exc)
    if isinstance(exc, ValidationError):
        message = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
    finish(
        ctx,
        command,
        code,
        {},
        diagnostics=[{"code": "invalid_input" if code == 2 else "error", "message": message}],
    )


def selected_project(path: Path | None):
    if path is None:
        path = Path("faultline.project.yaml")
        if not path.exists() and not path.is_symlink():
            return None
    return load_project(path)


def experiment_inputs(ctx, scenario):
    loaded = selected_project(ctx.obj["project"])
    if loaded is None:
        return load_scenario(Path(scenario)), None
    return loaded.load_scenario(scenario), loaded.project


@app.command()
def doctor(ctx: typer.Context):
    """Read-only platform, daemon, and image preflight; never pulls images."""
    try:
        loaded = selected_project(ctx.obj["project"])
        adapter, identity = resolve_adapter(loaded.project if loaded else None)
        if loaded is None:
            client, info = client_and_preflight()
            client.close()
        else:
            checked = {}
            for variant in loaded.project.variants:
                client, details = client_and_preflight(project=loaded.project, variant=variant)
                client.close()
                checked[variant] = details
            info = {"project": loaded.project.identity(), "variants": checked}
        info["adapter"] = {"description": adapter.description.model_dump(), "identity": identity}
    except Exception as exc:
        input_error(ctx, "doctor", exc)
    finish(ctx, "doctor", 0, info)


@app.command()
def validate(ctx: typer.Context, scenario: str):
    """Validate and materialize an adapter scenario without starting Docker."""
    try:
        value, project = experiment_inputs(ctx, scenario)
        adapter, identity = resolve_adapter(project, value)
    except Exception as exc:
        input_error(ctx, "validate", exc)
    finish(
        ctx,
        "validate",
        0,
        {
            "scenario_id": value.id,
            "profile": value.adapter_profile,
            "operations": len(value.all_operations),
            "faults": len(value.faults),
            "adapter": adapter.description.model_dump(),
            "adapter_identity": identity,
            "project": project.identity() if project else None,
            "effective_limits": value.limits.model_dump(),
            "plan_hash": digest(value.plan()),
        },
    )


@app.command()
def run(
    ctx: typer.Context,
    scenario: str,
    variant: Annotated[
        str | None,
        typer.Option(help="declared application variant; may omit for a single-variant project"),
    ] = None,
    repeat: Annotated[int, typer.Option(min=1, max=100)] = 1,
):
    """Run a bounded experiment; repetitions never reuse application storage."""
    try:
        value, project = experiment_inputs(ctx, scenario)
        if project is not None:
            variant = project.select_variant(variant)
        elif variant not in {"unsafe", "correct"}:
            raise ValueError("variant must be unsafe or correct")
        resolve_adapter(project, value, variant)
        client, info = client_and_preflight(project=project, variant=variant)
    except Exception as exc:
        input_error(ctx, "run", exc)
    group = (
        Group(ctx.obj["output"], "repeated_run", scheduled=repeat, details={"variant": variant})
        if repeat > 1
        else None
    )
    execute_series(
        ctx, "run", value, variant, project, client, info, repeat, group, lineage={"origin": "run"}
    )


def install_interrupts() -> dict:
    """SIGTERM follows the same cleanup path as Ctrl+C; a repeated signal is ignored."""
    requested = False

    def interrupt(_signum, _frame):
        nonlocal requested
        if not requested:
            requested = True
            raise KeyboardInterrupt

    return {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}


def execute_series(
    ctx, command, scenario, variant, project, client, info, repeat, group, *, lineage, extra=None
):
    """Serial fresh-environment runs; stops early on ERROR or interruption."""
    paths, reports = [], []
    if group is not None:
        lineage = {**lineage, "group_id": group.id}
    previous = install_interrupts()
    status = "completed"
    try:
        for index in range(repeat):
            typer.echo(f"{command} {index + 1}/{repeat}: {scenario.id} ({variant})", err=True)
            path, result = run_one(
                scenario,
                variant,
                ctx.obj["output"],
                client=client,
                platform_info=info,
                project=project,
                lineage=lineage,
            )
            paths.append(path)
            reports.append(result)
            if group is not None:
                group.add(result)
            if result["lifecycle"] == "interrupted" or result["verdict"] == "ERROR":
                status = "stopped_early"
                break
    except ActiveRunError as exc:
        if group is not None:
            group.close("refused")
        input_error(ctx, command, exc)
    except (Exception, KeyboardInterrupt) as exc:
        if group is not None:
            group.close("interrupted" if isinstance(exc, KeyboardInterrupt) else "harness_error")
        input_error(ctx, command, exc, 130 if isinstance(exc, KeyboardInterrupt) else 4)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        client.close()
    interrupted = any(r["lifecycle"] == "interrupted" for r in reports)
    if group is not None:
        summary = group.close("interrupted" if interrupted else status)
    else:
        summary = summarize(reports, repeat, None)
    summary = {"project": project.identity() if project else None, **summary, **(extra or {})}
    if len(reports) == 1:
        summary["coverage"] = reports[0]["coverage"]
        summary["violations"] = [r for r in reports[0]["invariants"] if r["status"] == "FAIL"]
    finish(
        ctx,
        command,
        130 if interrupted else EXIT_CODES[summary["verdict"] or "ERROR"],
        summary,
        paths=paths,
        run_ids=[r["run_id"] for r in reports],
        group_id=group.id if group else None,
        diagnostics=[d for r in reports for d in r["diagnostics"]],
    )


def select_signature(report: dict, requested: str | None) -> tuple[str | None, list[dict]]:
    """Pick the reproduction target; several candidates require an explicit choice."""
    catalog = failure_signatures(report)
    if requested is not None:
        if requested not in catalog:
            raise ValueError(f"signature not found in source run; choices: {sorted(catalog)}")
        return requested, []
    if len(catalog) == 1:
        return next(iter(catalog)), []
    if not catalog:
        return None, []
    return None, [
        {
            "code": "multiple_signatures",
            "message": "Source has several violation signatures; pass --signature to count "
            "reproductions",
            "details": catalog,
        }
    ]


def rejected(ctx, command, code, exc):
    finish(
        ctx,
        command,
        2,
        {"differences": exc.differences},
        diagnostics=[{"code": code, "message": str(exc)}],
    )


@app.command()
def replay(
    ctx: typer.Context,
    bundle: str,
    repeat: Annotated[int, typer.Option(min=1, max=100)] = 5,
    signature: Annotated[str | None, typer.Option(help="target violation signature")] = None,
):
    """Strictly re-execute a saved plan in fresh environments; no overrides accepted."""
    try:
        source = load_source(resolve_bundle(bundle, ctx.obj["output"]))
        target, notes = select_signature(source.report, signature)
        scenario = replay_scenario(source)
        client, info = client_and_preflight(project=source.project, variant=source.variant)
        try:
            check_platform(source, info)
        except BaseException:
            client.close()
            raise
    except ReplayRejected as exc:
        rejected(ctx, "replay", "replay_incompatible", exc)
    except Exception as exc:
        input_error(ctx, "replay", exc)
    details = {
        "source_run_id": source.run_id,
        "source_plan_hash": source.manifest["plan_hash"],
        "variant": source.variant,
    }
    group = Group(ctx.obj["output"], "replay", scheduled=repeat, signature=target, details=details)
    for note in notes:
        typer.echo(f"{note['code']}: {note['message']}", err=True)
    execute_series(
        ctx,
        "replay",
        scenario,
        source.variant,
        source.project,
        client,
        info,
        repeat,
        group,
        lineage={"origin": "replay", **details},
        extra={"source_run_id": source.run_id},
    )


@app.command()
def compare(
    ctx: typer.Context,
    bundle: str,
    variant: Annotated[str, typer.Option(help="explicitly chosen comparison variant")],
    repeat: Annotated[int, typer.Option(min=1, max=100)] = 5,
    signature: Annotated[str | None, typer.Option(help="target violation signature")] = None,
):
    """Run a saved plan against a chosen variant; differences are recorded, not hidden."""
    try:
        source = load_source(resolve_bundle(bundle, ctx.obj["output"]))
        target, _notes = select_signature(source.report, signature)
        loaded = selected_project(ctx.obj["project"])
        project = loaded.project if loaded else None
        if project is not None:
            variant = project.select_variant(variant)
        elif variant not in {"unsafe", "correct"}:
            raise ValueError("variant must be unsafe or correct")
        scenario, identity = comparison_scenario(source, project, variant)
        client, info = client_and_preflight(project=project, variant=variant)
    except ReplayRejected as exc:
        rejected(ctx, "compare", "comparison_invalid", exc)
    except Exception as exc:
        input_error(ctx, "compare", exc)
    found = comparison_differences(source, scenario, identity, info, variant)
    details = {
        "source_run_id": source.run_id,
        "source_plan_hash": source.manifest["plan_hash"],
        "source_variant": source.variant,
        "variant": variant,
        "differences": found,
    }
    group = Group(
        ctx.obj["output"], "comparison", scheduled=repeat, signature=target, details=details
    )
    execute_series(
        ctx,
        "compare",
        scenario,
        variant,
        project,
        client,
        info,
        repeat,
        group,
        lineage={
            "origin": "comparison",
            "source_run_id": source.run_id,
            "source_plan_hash": source.manifest["plan_hash"],
            "comparison": {"differences": found},
        },
        extra={"source_run_id": source.run_id, "differences": found},
    )


@app.command()
def reduce(
    ctx: typer.Context,
    bundle: str,
    signature: Annotated[str | None, typer.Option(help="target violation signature")] = None,
    max_runs: Annotated[
        int, typer.Option(min=1, max=MAX_RUNS, help="trial budget, including baseline")
    ] = DEFAULT_MAX_RUNS,
    max_duration_ms: Annotated[
        int, typer.Option(min=1, max=MAX_DURATION_MS, help="wall budget including cleanup")
    ] = DEFAULT_MAX_DURATION_MS,
):
    """Search for a smaller plan that still reproduces one selected violation."""
    try:
        source = load_source(resolve_bundle(bundle, ctx.obj["output"]))
        catalog = failure_signatures(source.report)
        if signature is None and len(catalog) > 1:
            # CI must never be prompted; print every choice and refuse.
            finish(
                ctx,
                "reduce",
                2,
                {"choices": catalog},
                diagnostics=[
                    {
                        "code": "signature_required",
                        "message": "Several violation signatures exist; pass --signature",
                    }
                ],
            )
        target, _notes = select_signature(source.report, signature)
        if target is None:
            raise ValueError("source run has no violation signature to reduce")
        original = replay_scenario(source)
        client, info = client_and_preflight(project=source.project, variant=source.variant)
        try:
            check_platform(source, info)
        except BaseException:
            client.close()
            raise
    except typer.Exit:
        raise
    except ReplayRejected as exc:
        rejected(ctx, "reduce", "reduction_environment_incompatible", exc)
    except Exception as exc:
        input_error(ctx, "reduce", exc)

    def validate(raw):
        candidate = Scenario.from_plan({**source.plan, "scenario": raw}, source.project)
        resolve_adapter(source.project, candidate, source.variant)
        return candidate

    def trial(scenario, purpose):
        typer.echo(f"reduce {reducer.runs_used}/{max_runs}: {purpose}", err=True)
        return run_one(
            scenario,
            source.variant,
            ctx.obj["output"],
            client=client,
            platform_info=info,
            project=source.project,
            lineage={
                "origin": "reduction",
                "source_run_id": source.run_id,
                "source_plan_hash": source.manifest["plan_hash"],
                "group_id": reducer.id,
                "trial_purpose": purpose,
            },
        )

    previous = install_interrupts()
    try:
        reducer = Reducer(
            ctx.obj["output"],
            source,
            target,
            validate=validate,
            trial=trial,
            max_runs=max_runs,
            max_duration_ms=max_duration_ms,
            artifact_bytes=original.limits.artifact_bytes,
            environment=info,
        )
        record = reducer.run()
    except KeyboardInterrupt as exc:
        input_error(ctx, "reduce", exc, 130)
    except Exception as exc:
        input_error(ctx, "reduce", exc, 4)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        client.close()
    summary = {
        key: record[key]
        for key in (
            "outcome",
            "termination_reason",
            "target_signature",
            "target",
            "original_objective",
            "best",
            "budget",
        )
    }
    summary["verdict"] = record["outcome"]
    summary["candidates_tried"] = len(record["candidates"])
    for name in ("baseline", "validation"):
        if record[name]:
            summary[name] = {k: v for k, v in record[name].items() if k != "trials"}
    finish(
        ctx,
        "reduce",
        REDUCTION_EXIT[record["outcome"]],
        summary,
        run_ids=record["run_ids"],
        group_id=record["group_id"],
    )


def read_group(value: str, output: Path) -> dict | None:
    """A UUID naming a group record (and no run) shows that group's full breakdown."""
    try:
        identifier = str(UUID(value))
    except ValueError:
        return None
    path = output / "groups" / f"{identifier}.json"
    if (output / "runs" / identifier).exists() or not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > 4194304:
        raise ValueError("group record exceeds size limit")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("group_schema_version") != 1 or record.get("group_id") != identifier:
        raise ValueError("unsupported or mismatched group record")
    return record


def resolve_bundle(value: str, output: Path) -> Path:
    try:
        return output / "runs" / str(UUID(value))
    except ValueError:
        return Path(value)


@app.command()
def report(ctx: typer.Context, bundle: str):
    """Verify artifact hashes and inspect a run without Docker or adapter execution."""
    try:
        group = read_group(bundle, ctx.obj["output"])
        path = resolve_bundle(bundle, ctx.obj["output"])
        if group is None:
            _manifest, result = read_bundle(path)
    except Exception as exc:
        input_error(ctx, "report", exc, 4)
    if group is not None:
        finish(
            ctx,
            "report",
            130 if group["interrupted"] else EXIT_CODES[group["verdict"] or "ERROR"],
            group,
            run_ids=group["run_ids"],
            group_id=group["group_id"],
        )
    finish(
        ctx,
        "report",
        EXIT_CODES[result["verdict"]],
        result,
        paths=[path],
        run_ids=[result["run_id"]],
    )


@app.command()
def clean(
    ctx: typer.Context,
    run: Annotated[str | None, typer.Option(help="UUID of a recorded run")] = None,
    stale: Annotated[
        bool, typer.Option(help="Recover non-live recorded runs in this output root")
    ] = False,
):
    """Remove recorded owned leftovers; preserve experiments and save separate cleanup receipts."""
    try:
        if (run is None) == (not stale):
            raise ValueError("Specify exactly one of --run RUN_ID or --stale")
        run_id = str(UUID(run)) if run is not None else None
        with execution_lock(ctx.obj["output"]):
            manifests = recovery_manifests(ctx.obj["output"], run_id)
            if manifests:
                client, _info = client_and_preflight(require_images=False)
                try:
                    results, paths = recover(ctx.obj["output"], manifests, client)
                except Exception as exc:
                    input_error(ctx, "clean", exc, 4)
                finally:
                    client.close()
            else:
                results, paths = [], []
    except Exception as exc:
        if isinstance(exc, typer.Exit):
            raise
        input_error(ctx, "clean", exc)
    complete = all(result["complete"] for result in results)
    finish(
        ctx,
        "clean",
        0 if complete else 4,
        {
            "complete": complete,
            "runs_checked": len(results),
            "remaining_resources": [r for result in results for r in result["remaining_resources"]],
        },
        paths=paths,
        run_ids=[result["run_id"] for result in results],
    )
