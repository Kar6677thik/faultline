"""Acknowledged one-shot fault actions on recorded, ownership-checked resources."""

import json
import time

CHECKPOINT_PATH = "/_faultline/checkpoints"
CHECKPOINT_POLL_SECONDS = 0.05


class FaultController:
    def __init__(self, environment, scenario, emit, applied, *, clock=time.monotonic):
        self.env, self.emit, self.applied, self.clock = environment, emit, applied, clock
        self.rules = [f for f in scenario.faults if f.kind != "response_suppress"]
        self.fired, self.active = set(), {}
        self.restarted = set()
        self.started = None
        self.routes = {op.id: op.route for op in scenario.all_operations}
        # attempt_id -> checkpoint fault, only while that attempt is in flight.
        self.watching = {}
        self.last_poll = 0.0

    def start(self):
        self.started = self.clock()
        for fault in self.rules:
            self.emit("fault_requested", fault.model_dump(), fault_id=fault.id)

    def on_event(self, kind, payload, **refs):
        for fault in self.rules:
            trigger = fault.trigger
            bound = refs.get("attempt_id") == f"{trigger.operation_id}/{trigger.attempt}"
            if trigger.event == "adapter_checkpoint" and fault.id not in self.fired and bound:
                # Watch the instrumented checkpoint only while the bound attempt is admitted.
                if kind == "attempt_dispatched":
                    self.watching[refs["attempt_id"]] = fault
                elif kind == "client_outcome":
                    self.watching.pop(refs["attempt_id"], None)
                continue
            if (
                fault.id not in self.fired
                and trigger.event == kind
                and trigger.operation_id == refs.get("operation_id")
                and refs.get("attempt_id") == f"{trigger.operation_id}/{trigger.attempt}"
            ):
                self.apply(fault, {"event": kind, **refs})

    def poll_checkpoints(self):
        """Ask the instrumented application, via the worker, which checkpoints it reached.

        The application never reaches the control network; the worker relays its answer.
        A checkpoint message only authorizes the crash. It is never evidence of a
        monetary violation, which the verifier still derives from raw state.
        """
        if not self.watching or self.clock() - self.last_poll < CHECKPOINT_POLL_SECONDS:
            return
        self.last_poll = self.clock()
        attempt_id, fault = next(iter(self.watching.items()))
        operation_id = fault.trigger.operation_id
        reply = self.env.call(
            "worker",
            "http",
            json.dumps(
                {
                    "route": self.routes[operation_id],
                    "method": "GET",
                    "path": CHECKPOINT_PATH,
                    "timeout_ms": 1000,
                }
            ),
            timeout=3,
        )
        if reply.get("outcome") != "http_response" or reply.get("status") != 200:
            return
        reached = reply.get("body", {}).get("checkpoints", [])
        for entry in reached if isinstance(reached, list) else []:
            if (
                isinstance(entry, dict)
                and entry.get("checkpoint") == fault.trigger.checkpoint
                and entry.get("attempt_id") == attempt_id
            ):
                self.watching.pop(attempt_id)
                refs = {"operation_id": operation_id, "attempt_id": attempt_id}
                self.emit(
                    "adapter_checkpoint",
                    {"checkpoint": entry["checkpoint"], "acknowledgment": entry},
                    fault_id=fault.id,
                    **refs,
                )
                self.apply(
                    fault,
                    {"event": "adapter_checkpoint", "checkpoint": entry["checkpoint"], **refs},
                )
                return

    def tick(self):
        self.poll_checkpoints()
        for fault, deadline in list(self.active.values()):
            if self.clock() >= deadline:
                self.heal(fault)
        for fault in self.rules:
            offset = fault.trigger.offset_ms
            if (
                fault.id not in self.fired
                and offset is not None
                and self.clock() >= self.started + offset / 1000
            ):
                self.apply(fault, {"offset_ms": offset})

    def pending(self):
        return (
            bool(self.active)
            or bool(self.watching)
            or any(f.id not in self.fired and f.trigger.offset_ms is not None for f in self.rules)
        )

    def proxy(self, action, fault):
        self.env.assert_owned(self.env.containers["proxy"])
        value = {
            "action": action,
            "id": fault.id,
            "direction": fault.direction,
            "latency_ms": fault.latency_ms,
        }
        result = self.env.call("worker", "proxy", json.dumps(value), timeout=5)
        if result.get("acknowledged") is not True or result.get("action") != action:
            raise RuntimeError("missing proxy acknowledgment")
        return result

    def apply(self, fault, trigger):
        try:
            self._apply(fault, trigger)
        except Exception:
            self.emit(
                "fault_failed", {"kind": fault.kind, "phase": "application"}, fault_id=fault.id
            )
            raise

    def _apply(self, fault, trigger):
        self.fired.add(fault.id)
        self.emit("fault_eligible", {"trigger": trigger}, fault_id=fault.id)
        # A failed/ambiguous application still needs a healing attempt.
        self.active[fault.id] = (fault, float("inf"))
        requested = self.clock()
        resource = self.env.containers[
            "application" if fault.kind == "process_restart" else "proxy"
        ]
        self.env.assert_owned(resource)
        if fault.kind == "process_restart":
            resource.kill(signal="SIGKILL")
            resource.wait(timeout=5)
            resource.reload()
            if resource.attrs["State"]["Running"]:
                raise RuntimeError("application kill was not acknowledged")
            acknowledgment = {"stopped": True, "image": resource.attrs["Image"]}
            duration = fault.downtime_ms
        else:
            acknowledgment = self.proxy(
                "latency" if fault.kind == "proxy_latency" else "disable", fault
            )
            duration = fault.duration_ms
        now = self.clock()
        self.active[fault.id] = (fault, now + duration / 1000)
        self.emit(
            "fault_applied",
            {
                "kind": fault.kind,
                "semantics_version": 1,
                "resource_id": resource.id,
                "trigger": trigger,
                "acknowledgment": acknowledgment,
                "requested_monotonic_ns": int(requested * 1e9),
                "applied_monotonic_ns": int(now * 1e9),
                "parameters": fault.model_dump(),
            },
            fault_id=fault.id,
        )
        self.applied.add(fault.id)

    def heal(self, fault):
        try:
            self._heal(fault)
        except Exception:
            self.emit("fault_failed", {"kind": fault.kind, "phase": "healing"}, fault_id=fault.id)
            raise

    def _heal(self, fault):
        if fault.kind == "process_restart":
            resource = self.env.containers["application"]
            self.env.assert_owned(resource)
            if fault.id not in self.restarted:
                resource.start()
                self.restarted.add(fault.id)
            if any(rule.kind.startswith("proxy_") for rule, _ in self.active.values()):
                return
            self.env.wait_application_ready()
            acknowledgment = {"ready": True, "resource_id": resource.id}
        else:
            acknowledgment = self.proxy(
                "remove" if fault.kind == "proxy_latency" else "enable", fault
            )
        self.emit(
            "fault_healed",
            {
                "kind": fault.kind,
                "acknowledgment": acknowledgment,
                "healed_monotonic_ns": int(self.clock() * 1e9),
            },
            fault_id=fault.id,
        )
        self.active.pop(fault.id)

    def heal_all(self):
        for fault, _deadline in sorted(
            self.active.values(), key=lambda item: item[0].kind == "process_restart"
        ):
            self.heal(fault)
