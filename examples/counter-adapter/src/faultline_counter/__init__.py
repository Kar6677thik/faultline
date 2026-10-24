"""Public hooks for an independent HTTP/PostgreSQL domain; no OrdersAdapter inheritance."""

from faultline.models import digest


class CounterAdapter:
    def describe(self):
        return {
            "boundary_version": 1,
            "adapter_id": "counter_example",
            "profile": "counter_v1",
            "fixture": "empty_counter",
            "fixture_version": 1,
            "fixture_hash": digest({"counter": [{"total": 0}], "increments": []}),
            "operations": ["increment"],
            "faults": ["response_suppress"],
            "invariants": {"counter.exactly_once": 1},
            "variants": ["correct", "unsafe"],
            "concurrency": 8,
            "authority": "postgresql",
            "quiescence": "graceful_process_exit_and_database_sessions",
            "external_registration": True,
            "generators": {"counter.increments": 1},
        }

    def expand(self, generator, seed):
        if generator["kind"] != "counter.increments" or generator["parameters"]:
            raise ValueError("counter.increments version 1 takes no parameters")
        operations = []
        for index in range(generator["count"]):
            identity = f"{generator['id']}-{index + 1}"
            delta = 1 + int(digest(["counter-sha256-v1", seed, generator["id"], index]), 16) % 100
            operations.append(
                {
                    "id": identity,
                    "kind": "increment",
                    "logical_id": identity,
                    "route": "counter_api",
                    "request": {
                        "method": "POST",
                        "path": "/increments",
                        "headers": {"Idempotency-Key": identity},
                        "json": {"intent": identity, "delta": delta},
                    },
                }
            )
        return {"operations": operations, "version": 1, "algorithm": "counter-sha256-v1"}

    def validate(self, scenario):
        identities = {}
        for op in scenario.all_operations:
            body = op.request.body.model_dump()
            if (
                op.expectation != "success"
                or not isinstance(body, dict)
                or op.request.path != "/increments"
                or op.request.method != "POST"
                or op.route != "counter_api"
                or set(body) != {"intent", "delta"}
                or body["intent"] != op.logical_id
                or type(body["delta"]) is not int
                or not 1 <= body["delta"] <= 100
                or set(op.request.headers) != {"Idempotency-Key"}
            ):
                raise ValueError(
                    "counter requires a positive bounded increment and an idempotency key"
                )
            if op.logical_id in identities and identities[op.logical_id] != body["delta"]:
                raise ValueError("counter identity cannot have conflicting deltas")
            identities[op.logical_id] = body["delta"]

    def prepare(self, environment, variant):
        result = environment.call(
            "observer", "prepare", "", timeout=10, module="faultline_counter.storage"
        )
        if result != {"prepared": True}:
            raise ValueError("counter fixture not acknowledged")
        baseline = self.snapshot(environment, "baseline")
        if baseline["records"] != {"counter": [{"total": 0}], "increments": []}:
            raise ValueError("counter baseline mismatch")
        return baseline

    def readiness(self, environment):
        environment.wait_application_ready()

    def quiesce(self, environment, timeout_ms):
        app = environment.containers["application"]
        environment.assert_owned(app)
        app.stop(timeout=max(1, timeout_ms // 1000))
        app.reload()
        ready = not app.attrs["State"]["Running"] and app.attrs["State"]["ExitCode"] == 0
        return {"state": "ready" if ready else "unknown", "evidence": ["snapshots/final.json"]}

    def snapshot(self, environment, role):
        return environment.call(
            "observer", "snapshot", role, timeout=10, module="faultline_counter.storage"
        )

    def verify(self, scenario, baseline, final, attempts, *, workload_complete):
        # This independent oracle reads raw increments and the independently observed total.
        # It never calls the application, imports its transaction, or sees its variant.
        records = final["records"]
        if set(records) != {"counter", "increments"} or len(records["counter"]) != 1:
            raise ValueError("invalid counter projection")
        rows = records["increments"]
        if any(
            set(r) != {"entry_id", "intent", "delta"} or type(r["delta"]) is not int for r in rows
        ):
            raise ValueError("invalid increment row")
        identities = {
            op.logical_id: op.request.body.model_dump()["delta"] for op in scenario.all_operations
        }
        results = []
        for identity, delta in identities.items():
            selected = [r for r in rows if r["intent"] == identity]
            certain = final["complete"] and final["quiescent"] and workload_complete
            valid = (
                len(selected) == 1
                and selected[0]["delta"] == delta
                and records["counter"][0]["total"] == sum(r["delta"] for r in rows)
                and all(r["intent"] in identities for r in rows)
            )
            failures = (
                []
                if valid or not certain
                else [
                    {
                        "code": "counter_effect_mismatch",
                        "expected": {"count": 1, "delta": delta},
                        "observed": selected,
                        "evidence": ["snapshots/final.json#/records/increments"],
                    }
                ]
            )
            result = {
                "invariant_id": "counter.exactly_once",
                "version": 1,
                "logical_id": identity,
                "status": "UNKNOWN" if not certain else ("FAIL" if failures else "PASS"),
                "failures": failures,
                "evidence": ["snapshots/final.json"],
                "signatures": [digest(["counter.exactly_once", identity])] if failures else [],
            }
            if not certain:
                result["reason"] = "incomplete_evidence"
            results.append(result)
        return results
