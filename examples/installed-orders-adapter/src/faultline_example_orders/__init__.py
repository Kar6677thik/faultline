"""Packaging example, not an independent verifier or a second correct application.

Reusing the domain hooks keeps this example focused on registration and evidence.
The incomplete adapter deliberately violates the judge's contract for calibration.
"""

from faultline.adapters import OrdersAdapter


class ExampleOrdersAdapter(OrdersAdapter):
    def describe(self):
        return {
            **super().describe(),
            "adapter_id": "example_orders",
            "external_registration": True,
        }


class IncompleteOrdersAdapter(ExampleOrdersAdapter):
    def describe(self):
        return {**super().describe(), "adapter_id": "incomplete_orders"}

    def verify(self, scenario, baseline, final, attempts, *, workload_complete):
        findings = super().verify(
            scenario, baseline, final, attempts, workload_complete=workload_complete
        )
        # A partial list of passing grades must become ERROR, never PASS.
        return findings[:-1]
