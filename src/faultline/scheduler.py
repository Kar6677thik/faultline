"""Bounded admission with one coordinator owning state and durable event writes.

Only transport calls run in threads. Completion order can change eligibility, but
ties always use materialized declaration order; randomness never runs here.
"""

import queue
import threading
import time

from faultline.models import canonical_bytes


def execute_attempts(
    scenario,
    transport,
    emit,
    run_id,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    attempts=None,
    applied=None,
    controller=None,
):
    attempts = [] if attempts is None else attempts
    applied = set() if applied is None else applied
    operations = scenario.all_operations
    state = {op.id: {"index": 1, "phase": "waiting", "eligible": 0.0} for op in operations}
    released = set()
    responses = queue.Queue(maxsize=scenario.concurrency)
    started = clock()
    complete = True
    record = emit
    for fault in scenario.faults:
        if fault.kind == "response_suppress":
            record("fault_requested", fault.model_dump(), fault_id=fault.id)
    if controller is not None:
        controller.start()

        def emit(kind, payload, **refs):
            record(kind, payload, **refs)
            controller.on_event(kind, payload, **refs)

    def invoke(op, request):
        try:
            result = transport(request)
        except BaseException as exc:
            result = exc
        responses.put((op.id, result))

    def ready(op):
        return (
            all(state[dep]["phase"] == "done" for dep in op.after)
            and set(op.after_faults) <= applied
        )

    def finish(op, upstream, outcome):
        current = state[op.id]
        index = current["index"]
        refs = {"operation_id": op.id, "attempt_id": f"{op.id}/{index}"}
        attempt = {
            **refs,
            "outcome": outcome,
            "upstream": upstream,
            "duration_ms": round((clock() - current["dispatched"]) * 1000),
        }
        if "suppression" in current:
            attempt["cause"] = "injected_response_suppression"
            emit("fault_healed", {"one_shot": True}, fault_id=current.pop("suppression"), **refs)
        attempts.append(attempt)
        emit("client_outcome", {k: v for k, v in attempt.items() if k != "upstream"}, **refs)
        retry_outcome = f"http_{upstream['status']}" if outcome == "http_response" else outcome
        if retry_outcome in op.retry.on and index < op.retry.max_attempts:
            delay = op.retry.backoff_ms[index - 1]
            emit("retry_eligible", {"next_attempt": index + 1, "backoff_ms": delay}, **refs)
            current.update(index=index + 1, phase="waiting", eligible=clock() + delay / 1000)
        else:
            current["phase"] = "done"
            for unused in range(index + 1, op.retry.max_attempts + 1):
                emit("attempt_not_needed", {}, operation_id=op.id, attempt_id=f"{op.id}/{unused}")

    by_id = {op.id: op for op in operations}
    while any(s["phase"] != "done" for s in state.values()) or (
        controller and controller.pending()
    ):
        if (clock() - started) * 1000 >= scenario.limits.execution_ms:
            complete = False
            break
        if controller:
            controller.tick()
        # Drain only already-arrived results. A slow earlier request never blocks a later one.
        while True:
            try:
                op_id, upstream = responses.get_nowait()
            except queue.Empty:
                break
            if isinstance(upstream, BaseException):
                raise upstream
            op = by_id[op_id]
            current = state[op_id]
            refs = {"operation_id": op_id, "attempt_id": f"{op_id}/{current['index']}"}
            outcome = upstream["outcome"]
            if outcome == "http_response":
                emit("upstream_response_complete", upstream, **refs)
            if clock() >= current["deadline"]:
                outcome = "timeout"
            for fault in scenario.faults:
                if (
                    fault.kind == "response_suppress"
                    and fault.trigger.operation_id == op_id
                    and fault.trigger.attempt == current["index"]
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
                    current.update(phase="suppressed", suppression=fault.id, upstream=upstream)
                    break
            if current["phase"] != "suppressed":
                finish(op, upstream, outcome)
        for op in operations:
            current = state[op.id]
            if current["phase"] == "suppressed" and clock() >= current["deadline"]:
                finish(op, current.pop("upstream"), "timeout")
        for barrier in scenario.barriers:
            if barrier.id not in released and all(ready(by_id[m]) for m in barrier.members):
                emit("barrier_released", {"barrier_id": barrier.id, "members": barrier.members})
                released.add(barrier.id)
        active = sum(s["phase"] in {"inflight", "suppressed"} for s in state.values())
        for op in operations:
            current = state[op.id]
            if active >= scenario.concurrency:
                break
            if (
                current["phase"] != "waiting"
                or current["eligible"] > clock()
                or not ready(op)
                or (op.barrier is not None and op.barrier not in released)
            ):
                continue
            refs = {"operation_id": op.id, "attempt_id": f"{op.id}/{current['index']}"}
            headers = {
                **op.request.headers,
                "Content-Type": "application/json",
                "X-Faultline-Run-Id": run_id,
                "X-Faultline-Operation-Id": op.id,
                "X-Faultline-Attempt-Id": refs["attempt_id"],
            }
            request = {
                "route": op.route,
                "method": op.request.method,
                "path": op.request.path,
                "body": canonical_bytes(op.request.body.model_dump()).decode(),
                "headers": headers,
                "timeout_ms": op.timeout_ms,
            }
            emit(
                "attempt_dispatched",
                {"request": op.request.model_dump(by_alias=True), "in_flight": active + 1},
                **refs,
            )
            current.update(
                phase="inflight", dispatched=clock(), deadline=clock() + op.timeout_ms / 1000
            )
            active += 1
            if scenario.concurrency == 1 and clock is not time.monotonic:
                invoke(op, request)
            else:
                threading.Thread(target=invoke, args=(op, request), daemon=True).start()
        if all(s["phase"] == "done" for s in state.values()) and not (
            controller and controller.pending()
        ):
            break
        # No work can release an unacknowledged contingent fault gate.
        if (
            not active
            and not (controller and controller.pending())
            and not any(
                s["phase"] == "waiting"
                and ready(by_id[k])
                and (by_id[k].barrier is None or by_id[k].barrier in released)
                for k, s in state.items()
            )
        ):
            complete = False
            break
        if responses.empty():
            sleep(0.001)
    return attempts, applied, complete
