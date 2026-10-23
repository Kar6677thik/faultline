from faultline.engine import execute_attempts


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_script(scenario, responses):
    clock = Clock()
    requests, events = [], []
    iterator = iter(responses)

    def transport(request):
        requests.append(request)
        delay, response = next(iterator)
        clock.sleep(delay)
        return response

    result = execute_attempts(
        scenario,
        transport,
        lambda kind, payload, **refs: events.append((kind, payload, refs)),
        "run-id",
        clock=clock.time,
        sleep=clock.sleep,
    )
    return result, requests, events


def test_suppression_precedes_retry_and_preserves_request(scenario):
    response = {"outcome": "http_response", "status": 201, "body": {"order_id": "one"}}
    (attempts, applied, complete), requests, events = run_script(
        scenario, [(0.042, response), (0.020, response)]
    )
    assert complete and applied == {"hide_first_response"}
    assert [a["outcome"] for a in attempts] == ["timeout", "http_response"]
    assert attempts[0]["duration_ms"] == 1000
    assert requests[0]["body"] == requests[1]["body"]
    assert requests[0]["headers"]["Idempotency-Key"] == requests[1]["headers"]["Idempotency-Key"]
    assert (
        requests[0]["headers"]["X-Faultline-Attempt-Id"]
        != requests[1]["headers"]["X-Faultline-Attempt-Id"]
    )
    types = [e[0] for e in events]
    assert (
        types.index("fault_applied") < types.index("client_outcome") < types.index("retry_eligible")
    )


def test_wrong_status_is_not_suppressed_or_retried(scenario):
    result, requests, events = run_script(
        scenario, [(0.02, {"outcome": "http_response", "status": 409})]
    )
    assert not result[1]
    assert len(requests) == 1
    assert any(e[0] == "attempt_not_needed" for e in events)


def test_late_response_never_counts_as_applied_fault(scenario):
    response = {"outcome": "http_response", "status": 201, "body": {}}
    result, _, _ = run_script(scenario, [(1.2, response), (0.01, response)])
    assert not result[1]
    assert result[0][0]["outcome"] == "timeout"


def test_connection_failure_has_no_success_witness(scenario):
    result, requests, _ = run_script(
        scenario, [(0.1, {"outcome": "connection_error"}), (0.1, {"outcome": "connection_error"})]
    )
    assert len(requests) == 2
    assert not result[1]
