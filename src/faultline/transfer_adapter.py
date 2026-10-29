"""Bundled adapter for the transfer domain; no order code is imported or inherited."""

import json

from faultline import transfer_verifier
from faultline.models import digest

HELPER = "faultline.transfer_store"
EXPECTATIONS = {"success", "insufficient_funds", "idempotency_conflict", "transfer_conflict"}
EXPECTATIONS |= {"invalid_request"}


class TransfersAdapter:
    def describe(self):
        return {
            "boundary_version": 1,
            "adapter_id": "bundled_transfers",
            "profile": "transfers_v1",
            "fixture": "two_accounts",
            "fixture_version": 1,
            "fixture_hash": digest(transfer_verifier.fixture_records("two_accounts")),
            "operations": ["create_transfer"],
            "faults": ["response_suppress", "proxy_latency", "proxy_disconnect", "process_restart"],
            "invariants": dict.fromkeys(transfer_verifier.INVARIANTS, 1),
            "variants": ["correct", "non_atomic"],
            "concurrency": 8,
            "authority": "postgresql",
            "quiescence": "graceful_process_exit_and_database_sessions",
            "external_registration": False,
            # Only the non-atomic variant has a durable debit to pause after.
            "checkpoints": {"transfer.debit_committed": ["non_atomic"]},
            "readiness_checks": [
                "http_health",
                "postgres_ready",
                "worker_handshake",
                "observer_snapshot",
                "proxy_ready",
            ],
        }

    def validate(self, scenario):
        identities = {}
        for op in scenario.all_operations:
            request = op.request
            body = request.body.model_dump()
            headers = {k.lower(): v for k, v in request.headers.items()}
            if (
                op.expectation not in EXPECTATIONS
                or request.method != "POST"
                or request.path != "/transfers"
                or op.route != "transfers_api"
                or set(headers) != {"idempotency-key"}
                or not isinstance(body, dict)
                or set(body)
                != {"transfer_id", "from_account_id", "to_account_id", "amount_minor", "currency"}
                or body["transfer_id"] != op.logical_id
                or type(body["amount_minor"]) is not int
                or body["currency"] != "INR"
            ):
                raise ValueError("transfers require POST /transfers with a complete INR body")
            if op.expectation == "success":
                if body["amount_minor"] <= 0 or body["from_account_id"] == body["to_account_id"]:
                    raise ValueError(
                        "a successful transfer needs a positive amount and two parties"
                    )
                if identities.get(op.logical_id, body) != body:
                    raise ValueError("conflicting payloads require explicit conflict expectations")
                identities[op.logical_id] = body
        for name in scenario.checkpoints:
            if name != "transfer.debit_committed":
                raise ValueError("unknown transfer checkpoint")

    def prepare(self, environment, variant):
        selection = json.dumps({"variant": variant, "fixture": "two_accounts"})
        result = environment.call("observer", "prepare", selection, timeout=10, module=HELPER)
        if result != {"prepared": True, "fixture": "two_accounts"}:
            raise ValueError("transfer fixture was not acknowledged")
        baseline = self.snapshot(environment, "baseline")
        transfer_verifier.validate_baseline(baseline)
        return baseline

    def readiness(self, environment):
        environment.wait_application_ready()

    def quiesce(self, environment, timeout_ms):
        app = environment.containers["application"]
        environment.assert_owned(app)
        app.stop(timeout=max(1, timeout_ms // 1000))
        app.reload()
        stopped = not app.attrs["State"]["Running"] and app.attrs["State"]["ExitCode"] == 0
        return {"state": "ready" if stopped else "unknown", "evidence": ["history.ndjson"]}

    def snapshot(self, environment, role):
        snapshot = environment.call("observer", "snapshot", role, timeout=10, module=HELPER)
        transfer_verifier.validate_snapshot(snapshot)
        return snapshot

    def verify(self, scenario, baseline, final, attempts, *, workload_complete):
        return transfer_verifier.verify(
            scenario, baseline, final, attempts, workload_complete=workload_complete
        )
