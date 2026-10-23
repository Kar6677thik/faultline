"""CLI for the intentionally constrained first experiment milestone."""

import json
import signal
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from pydantic import ValidationError

from faultline.adapters import AdapterError, bundled_adapter
from faultline.artifacts import atomic_json, read_bundle
from faultline.engine import run_one
from faultline.environment import client_and_preflight
from faultline.locking import ActiveRunError, execution_lock
from faultline.models import EXIT_CODES, load_scenario
from faultline.recovery import recover, recovery_manifests

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Faultline M1: disposable order correctness experiments. Linux/WSL2 execution.",
)


@app.callback()
def options(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="Artifact root")] = Path(".faultline"),
    format: Annotated[str, typer.Option(help="text or json")] = "text",
    no_color: Annotated[bool, typer.Option()] = False,
):
    if format not in {"text", "json"}:
        raise typer.BadParameter("format must be text or json")
    ctx.obj = {"output": output.resolve(), "format": format}
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


@app.command()
def doctor(ctx: typer.Context):
    """Read-only platform, daemon, and image preflight; never pulls images."""
    try:
        client, info = client_and_preflight()
        client.close()
    except Exception as exc:
        input_error(ctx, "doctor", exc)
    finish(ctx, "doctor", 0, info)


@app.command()
def validate(ctx: typer.Context, scenario: Path):
    """Validate the supported M1 scenario subset without starting Docker."""
    try:
        value = load_scenario(scenario)
        adapter = bundled_adapter()
        adapter.validate(value)
    except Exception as exc:
        input_error(ctx, "validate", exc)
    finish(
        ctx,
        "validate",
        0,
        {
            "scenario_id": value.id,
            "profile": "milestone1",
            "operations": 1,
            "faults": len(value.faults),
            "adapter": adapter.description.model_dump(),
        },
    )


@app.command()
def run(
    ctx: typer.Context,
    scenario: Path,
    variant: Annotated[str, typer.Option(help="unsafe or correct")],
    repeat: Annotated[int, typer.Option(min=1, max=100)] = 1,
):
    """Run one purchase from a fresh fixture; repeats never reuse application storage."""
    try:
        if variant not in {"unsafe", "correct"}:
            raise ValueError("variant must be unsafe or correct")
        value = load_scenario(scenario)
        bundled_adapter().validate(value, variant)
        client, info = client_and_preflight()
    except Exception as exc:
        input_error(ctx, "run", exc)
    paths, reports = [], []
    group_id = str(uuid4()) if repeat > 1 else None
    # SIGTERM follows the same cleanup path as Ctrl+C.
    interrupt_requested = False

    def interrupt(_signum, _frame):
        nonlocal interrupt_requested
        if not interrupt_requested:
            interrupt_requested = True
            raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for index in range(repeat):
            typer.echo(f"Run {index + 1}/{repeat}: {value.id} ({variant})", err=True)
            path, result = run_one(
                value, variant, ctx.obj["output"], client=client, platform_info=info
            )
            paths.append(path)
            reports.append(result)
            if result["lifecycle"] == "interrupted" or result["verdict"] == "ERROR":
                break
    except ActiveRunError as exc:
        input_error(ctx, "run", exc)
    except (Exception, KeyboardInterrupt) as exc:
        input_error(ctx, "run", exc, 130 if isinstance(exc, KeyboardInterrupt) else 4)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        client.close()
    counts = {state: sum(r["verdict"] == state for r in reports) for state in EXIT_CODES}
    overall = next(state for state in ("ERROR", "FAIL", "INCONCLUSIVE", "PASS") if counts[state])
    summary = {
        "verdict": overall,
        "counts": counts,
        "scheduled": repeat,
        "started": len(reports),
        "unstarted": repeat - len(reports),
        "cleanup_complete": all(r["cleanup"]["complete"] for r in reports),
    }
    if len(reports) == 1:
        summary["coverage"] = reports[0]["coverage"]
        summary["violations"] = [r for r in reports[0]["invariants"] if r["status"] == "FAIL"]
    if group_id:
        group_path = ctx.obj["output"] / "groups" / f"{group_id}.json"
        group_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            group_path,
            {
                "group_schema_version": 1,
                "group_id": group_id,
                "kind": "repeated_run",
                "run_ids": [r["run_id"] for r in reports],
                **summary,
            },
        )
    code = 130 if any(r["lifecycle"] == "interrupted" for r in reports) else EXIT_CODES[overall]
    finish(
        ctx,
        "run",
        code,
        summary,
        paths=paths,
        run_ids=[r["run_id"] for r in reports],
        group_id=group_id,
        diagnostics=[d for r in reports for d in r["diagnostics"]],
    )


def resolve_bundle(value: str, output: Path) -> Path:
    try:
        return output / "runs" / str(UUID(value))
    except ValueError:
        return Path(value)


@app.command()
def report(ctx: typer.Context, bundle: str):
    """Verify artifact hashes and inspect a run without Docker or adapter execution."""
    try:
        path = resolve_bundle(bundle, ctx.obj["output"])
        _manifest, result = read_bundle(path)
    except Exception as exc:
        input_error(ctx, "report", exc, 4)
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
