"""One bounded purchase experiment, real environment, and independent final verification."""

import json
import time

from faultline.artifacts import Bundle
from faultline.environment import Environment, client_and_preflight
from faultline.models import canonical_bytes, verdict
from faultline.verifier import validate_baseline, verify


def execute_attempts(scenario, transport, emit, run_id, *, clock=time.monotonic, sleep=time.sleep):
    operation = scenario.operations[0]
    body = canonical_bytes(operation.request.body.model_dump()).decode()
    attempts = []
    applied = set()
    started = clock()
    complete = True
    for index in range(1, operation.retry.max_attempts + 1):
        if (clock() - started) * 1000 >= scenario.limits.execution_ms:
            complete = False
            break
        attempt_id = f"{operation.id}/{index}"
        refs = {"operation_id": operation.id, "attempt_id": attempt_id}
        headers = dict(
            operation.request.headers,
            **{
                "Content-Type": "application/json",
                "X-Faultline-Run-Id": run_id,
                "X-Faultline-Operation-Id": operation.id,
                "X-Faultline-Attempt-Id": attempt_id,
            },
        )
        emit("attempt_dispatched", {"request": operation.request.model_dump(by_alias=True)}, **refs)
        dispatched = clock()
        upstream = transport(
            {
                "method": "POST",
                "path": "/orders",
                "body": body,
                "headers": headers,
                "timeout_ms": operation.timeout_ms,
            }
        )
        elapsed = clock() - dispatched
        outcome = upstream["outcome"]
        if outcome == "http_response":
            emit("upstream_response_complete", upstream, **refs)
        # A late response can inform state/response checks, but cannot trigger this fault.
        if elapsed >= operation.timeout_ms / 1000:
            outcome = "timeout"
        for fault in scenario.faults:
            if (
                fault.trigger.attempt == index
                and upstream.get("status") == fault.trigger.status
                and outcome == "http_response"
                and fault.id not in applied
            ):
                emit("fault_eligible", {"kind": fault.kind}, fault_id=fault.id, **refs)
                emit(
                    "fault_applied",
                    {
                        "kind": fault.kind,
                        "upstream_status": upstream["status"],
                        "semantics": "client_response_suppression",
                    },
                    fault_id=fault.id,
                    **refs,
                )
                applied.add(fault.id)
                sleep(max(0, operation.timeout_ms / 1000 - (clock() - dispatched)))
                outcome = "timeout"
                emit("fault_healed", {"one_shot": True}, fault_id=fault.id, **refs)
        attempt = {
            "attempt_id": attempt_id,
            "outcome": outcome,
            "upstream": upstream,
            "duration_ms": round((clock() - dispatched) * 1000),
        }
        if index in {f.trigger.attempt for f in scenario.faults if f.id in applied}:
            attempt["cause"] = "injected_response_suppression"
        attempts.append(attempt)
        emit("client_outcome", {k: v for k, v in attempt.items() if k != "upstream"}, **refs)
        if outcome not in operation.retry.on:
            for unused in range(index + 1, operation.retry.max_attempts + 1):
                emit(
                    "attempt_not_needed",
                    {},
                    operation_id=operation.id,
                    attempt_id=f"{operation.id}/{unused}",
                )
            break
        if index < operation.retry.max_attempts:
            emit(
                "retry_eligible",
                {"next_attempt": index + 1, "backoff_ms": operation.retry.backoff_ms[index - 1]},
                **refs,
            )
            sleep(operation.retry.backoff_ms[index - 1] / 1000)
    return attempts, applied, complete


def run_one(scenario, variant, output, *, client=None, platform_info=None):
    owns_client = client is None
    if client is None:
        client, platform_info = client_and_preflight()
    bundle = Bundle(output, scenario, variant)
    bundle.manifest["platform"] = platform_info
    bundle.save_manifest()
    env = Environment(client, bundle, platform_info["images"])
    findings, diagnostics, attempts = [], [], []
    applied = set()
    complete = False
    interrupted = False
    cleanup = {"complete": False, "remaining_resources": []}
    try:
        bundle.stage("provisioning")
        env.provision(variant)
        baseline = env.snapshot("baseline")
        validate_baseline(baseline)
        bundle.write("snapshots/baseline.json", baseline)
        bundle.event("baseline_captured", {"content_hash": baseline["content_hash"]})
        env.start_application(variant)
        bundle.stage("ready")
        bundle.stage("executing")
        attempts, applied, complete = execute_attempts(
            scenario,
            lambda request: env.call(
                "worker", "http", json.dumps(request), timeout=request["timeout_ms"] / 1000 + 2
            ),
            bundle.event,
            bundle.run_id,
        )
        bundle.stage("draining")
        graceful = env.drain(scenario.limits.drain_ms)
        bundle.event(
            "quiescence",
            {"method": "graceful_process_exit_and_database_sessions", "graceful_exit": graceful},
        )
        bundle.stage("observing")
        final = env.snapshot("final")
        final["quiescent"] &= graceful
        bundle.write("snapshots/final.json", final)
        bundle.event("snapshot_captured", {"content_hash": final["content_hash"]})
        bundle.stage("verifying")
        findings = verify(scenario, baseline, final, attempts, workload_complete=complete)
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
    except Exception as exc:
        diagnostics.append(
            {
                "severity": "error",
                "code": "harness_error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        cleanup = env.cleanup()
        if not cleanup["complete"]:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "cleanup_failed",
                    "message": "Owned resources remain; use faultline clean --run",
                }
            )
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
        if owns_client:
            client.close()
    required = {fault.id for fault in scenario.faults if fault.required}
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
                "message": "Required response fault did not match an eligible response",
            }
        )
    report = {
        "verdict": verdict(
            findings, diagnostics, complete and required <= applied and not interrupted
        ),
        "lifecycle": "interrupted" if interrupted else "completed",
        "invariants": findings,
        "diagnostics": diagnostics,
        "coverage": coverage,
        "attempts": attempts,
        "cleanup": cleanup,
    }
    bundle.finalize(report)
    return bundle.path, report
