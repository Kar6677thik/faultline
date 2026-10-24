"""Bounded experiments, owned environments, and independent authoritative verification."""

import json
import time

from faultline.adapter_loader import resolve_adapter
from faultline.adapters import AdapterError
from faultline.artifacts import Bundle
from faultline.deadlines import DeadlineExceeded, RunDeadlines, check_alarm_available
from faultline.environment import Environment, client_and_preflight
from faultline.faults import FaultController
from faultline.locking import execution_lock
from faultline.models import verdict
from faultline.projects import Project
from faultline.scheduler import execute_attempts


def run_one(scenario, variant, output, *, client=None, platform_info=None, project=None):
    # Direct Python callers must exclude recovery just as CLI callers do.
    if project is not None:
        project = Project.model_validate(project.configuration())
        variant = project.select_variant(variant)
        scenario = project.scenario(scenario.model_dump(by_alias=True, exclude_unset=True))
    adapter, identity = resolve_adapter(project, scenario, variant)
    check_alarm_available()
    with execution_lock(output):
        owns_client = client is None
        if client is None:
            client, platform_info = client_and_preflight(project=project, variant=variant)
        try:
            if project is not None and (
                platform_info.get("project_configuration_hash")
                != project.identity()["configuration_hash"]
                or platform_info.get("variant") != variant
                or "application" not in platform_info.get("images", {})
            ):
                raise ValueError("preflight must resolve images for this exact project and variant")
            return _run_one(
                scenario,
                variant,
                output,
                adapter=adapter,
                adapter_identity=identity,
                client=client,
                platform_info=platform_info,
                project=project,
            )
        except DeadlineExceeded as exc:
            # Initialization/finalization may have no time to write a complete report.
            raise RuntimeError(f"{exc}; partial evidence retained under {output}/runs") from exc
        finally:
            if owns_client:
                client.close()


def _run_one(
    scenario,
    variant,
    output,
    *,
    adapter,
    adapter_identity,
    client=None,
    platform_info=None,
    project=None,
):
    limits = scenario.limits
    deadlines = RunDeadlines(limits)
    with deadlines.stage("initializing", limits.provisioning_ms, started=deadlines.started):
        bundle = Bundle(output, scenario, variant, project.configuration() if project else None)
        bundle.manifest["platform"] = platform_info
        bundle.save_manifest()
        env = Environment(client, bundle, platform_info["images"], project)
    findings, diagnostics, attempts = [], [], []
    applied = set()
    complete = False
    interrupted = False
    cleanup = {"complete": False, "remaining_resources": []}
    controller = FaultController(env, scenario, bundle.event, applied)
    try:
        with deadlines.stage("provisioning", limits.provisioning_ms, started=deadlines.started):
            bundle.stage("provisioning")
            identity = adapter_identity
            bundle.write(
                "adapter.json",
                {
                    "adapter_artifact_version": 1,
                    "description": adapter.description.model_dump(),
                    "identity": identity,
                },
            )
            bundle.manifest["adapter"] = {
                "adapter_id": adapter.description.adapter_id,
                "boundary_version": adapter.description.boundary_version,
                "package": identity["package"],
                "package_version": identity["package_version"],
                "content_hash": identity["content_hash"],
                "artifact": "adapter.json",
            }
            bundle.save_manifest()
            bundle.event("adapter_selected", bundle.manifest["adapter"])
            env.provision()
            baseline = adapter.prepare(env, variant)
            bundle.write("snapshots/baseline.json", baseline)
            bundle.event("baseline_captured", {"content_hash": baseline["content_hash"]})
            env.start_application(variant)
            adapter.readiness(env)
            bundle.stage("ready")
        try:
            with deadlines.stage("executing", limits.execution_ms):
                bundle.stage("executing")
                attempts, applied, complete = execute_attempts(
                    scenario,
                    lambda request: env.call(
                        "worker",
                        "http",
                        json.dumps(request),
                        timeout=request["timeout_ms"] / 1000 + 2,
                    ),
                    bundle.event,
                    bundle.run_id,
                    attempts=attempts,
                    applied=applied,
                    controller=controller,
                )
        except DeadlineExceeded as exc:
            complete = False
            diagnostics.append(exc.diagnostic("warning"))
        graceful = False
        try:
            with deadlines.stage("draining", limits.drain_ms):
                bundle.stage("draining")
                controller.heal_all()
                quiescence = adapter.quiesce(env, limits.drain_ms)
                bundle.event("adapter_quiescence", quiescence)
                while quiescence["state"] == "pending":
                    time.sleep(0.05)
                    quiescence = adapter.quiesce(env, limits.drain_ms)
                    bundle.event("adapter_quiescence", quiescence)
                graceful = quiescence["state"] == "ready"
        except DeadlineExceeded as exc:
            graceful = False
            diagnostics.append(exc.diagnostic("warning"))
        with deadlines.stage("observing", limits.observation_ms):
            bundle.event(
                "quiescence",
                {
                    "method": "graceful_process_exit_and_database_sessions",
                    "graceful_exit": graceful,
                },
            )
            bundle.stage("observing")
            final = adapter.snapshot(env, "final")
            final["quiescent"] &= graceful
            bundle.write("snapshots/final.json", final)
            bundle.event("snapshot_captured", {"content_hash": final["content_hash"]})
        with deadlines.stage("verifying", limits.verification_ms):
            bundle.stage("verifying")
            findings = adapter.verify(
                scenario, baseline, final, attempts, workload_complete=complete
            )
            for finding in findings:
                bundle.event("invariant_evaluated", finding)
        if not graceful:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "drain_incomplete",
                    "message": "Application did not exit gracefully; final state is uncertain",
                }
            )
    except KeyboardInterrupt:
        interrupted = True
        diagnostics.append(
            {
                "severity": "warning",
                "code": "interrupted",
                "message": "Run interrupted; no new work admitted",
            }
        )
    except DeadlineExceeded as exc:
        diagnostics.append(exc.diagnostic())
    except AdapterError as exc:
        diagnostics.append(exc.diagnostic())
    except Exception as exc:
        diagnostics.append(
            {
                "severity": "error",
                "code": "harness_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        cleanup_end, finalization_end = deadlines.cleanup_windows()
        try:
            with deadlines.until("cleanup", cleanup_end, "cleanup"):
                try:
                    controller.heal_all()
                except Exception:
                    diagnostics.append(
                        {
                            "severity": "error",
                            "code": "fault_healing_failed",
                            "message": "Fault recovery failed before teardown",
                        }
                    )
                cleanup = env.cleanup()
        except (DeadlineExceeded, KeyboardInterrupt, Exception) as exc:
            if isinstance(exc, KeyboardInterrupt):
                interrupted = True
            cleanup = {
                "complete": False,
                "remaining_resources": env.cleanup_remaining or bundle.manifest["resources"],
            }
            diagnostics.append(
                exc.diagnostic()
                if isinstance(exc, DeadlineExceeded)
                else {
                    "severity": "error",
                    "code": "cleanup_interrupted",
                    "message": "Cleanup interrupted or failed; recorded resources may remain",
                }
            )
        if not cleanup["complete"]:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "cleanup_failed",
                    "message": "Owned resources remain; use faultline clean --run",
                }
            )
    with deadlines.until("finalizing", finalization_end, "cleanup_and_total"):
        return finalize_run(
            bundle,
            scenario,
            findings,
            diagnostics,
            attempts,
            applied,
            complete,
            interrupted,
            cleanup,
            deadlines,
        )


def finalize_run(
    bundle,
    scenario,
    findings,
    diagnostics,
    attempts,
    applied,
    complete,
    interrupted,
    cleanup,
    deadlines,
):
    try:
        bundle.event("cleanup", cleanup)
    except OSError:
        diagnostics.append(
            {
                "severity": "error",
                "code": "artifact_write_failed",
                "message": "Could not append cleanup evidence",
            }
        )
    required = {fault.id for fault in scenario.faults if fault.required}
    for fault in scenario.faults:
        if fault.id not in applied:
            bundle.event(
                "fault_unapplied",
                {"kind": fault.kind, "required": fault.required},
                fault_id=fault.id,
            )
    coverage = {
        "required_total": len(required),
        "required_applied": len(required & applied),
        "applied": sorted(applied),
        "unapplied": [f.id for f in scenario.faults if f.id not in applied],
        "workload_complete": complete,
    }
    if required - applied:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "fault_not_triggered",
                "message": "A required fault has no successful application acknowledgment",
            }
        )
    report = {
        "project": bundle.manifest.get("project"),
        "adapter": bundle.manifest.get("adapter"),
        "verdict": verdict(
            findings, diagnostics, complete and required <= applied and not interrupted
        ),
        "lifecycle": "interrupted" if interrupted else "completed",
        "invariants": findings,
        "diagnostics": diagnostics,
        "coverage": coverage,
        "attempts": attempts,
        "cleanup": cleanup,
        "timing": {
            "total_budget_ms": scenario.limits.total_ms,
            "cleanup_budget_ms": scenario.limits.cleanup_ms,
            "elapsed_before_report_ms": round((time.monotonic() - deadlines.started) * 1000),
            # Finalization's elapsed duration is not known until this report is persisted.
            "stages": [dict(stage) for stage in deadlines.stages if stage["stage"] != "finalizing"],
        },
    }
    bundle.finalize(report)
    return bundle.path, report
