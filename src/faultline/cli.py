"""CLI for the intentionally constrained first experiment milestone."""

import json
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from pydantic import ValidationError

from faultline.artifacts import atomic_json, read_bundle
from faultline.engine import run_one
from faultline.environment import Environment, client_and_preflight
from faultline.models import EXIT_CODES, load_scenario

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


@contextmanager
def execution_lock(output):
    import fcntl

    output.mkdir(parents=True, exist_ok=True)
    with (output / "execution.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Faultline operation owns this output directory") from exc
        yield


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
        with execution_lock(ctx.obj["output"]):
            for index in range(repeat):
                typer.echo(f"Run {index + 1}/{repeat}: {value.id} ({variant})", err=True)
                path, result = run_one(
                    value, variant, ctx.obj["output"], client=client, platform_info=info
                )
                paths.append(path)
                reports.append(result)
                if result["lifecycle"] == "interrupted" or result["verdict"] == "ERROR":
                    break
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
def clean(ctx: typer.Context, run: Annotated[str, typer.Option(help="UUID of a recorded run")]):
    """Remove only owned leftovers for one run; preserve its evidence."""
    try:
        run_id = str(UUID(run))
        path = ctx.obj["output"] / "runs" / run_id
        manifest_path = path / "manifest.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 1048576:
            raise ValueError("unsafe manifest")
        manifest = json.loads(manifest_path.read_text())
        if manifest["run_id"] != run_id:
            raise ValueError("run identity mismatch")
        client, info = client_and_preflight(require_images=False)
        try:
            with execution_lock(ctx.obj["output"]):
                from types import SimpleNamespace

                env = Environment(
                    client, SimpleNamespace(manifest=manifest, run_id=run_id), info["images"]
                )
                result = env.cleanup()
        finally:
            client.close()
    except Exception as exc:
        input_error(ctx, "clean", exc)
    finish(ctx, "clean", 0 if result["complete"] else 4, result)
