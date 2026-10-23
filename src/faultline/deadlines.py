"""Linux main-thread stage alarms, with an absolute total budget and cleanup reserve."""

import signal
import threading
import time
from contextlib import contextmanager


class DeadlineExceeded(BaseException):
    """Control flow, not an HTTP timeout that transport libraries may catch and retry."""

    def __init__(self, stage, scope):
        self.stage, self.scope = stage, scope
        super().__init__(f"{scope} deadline exceeded during {stage}")

    def diagnostic(self, severity="error"):
        return {
            "severity": severity,
            "code": "deadline_exceeded",
            "stage": self.stage,
            "scope": self.scope,
            "message": str(self),
        }


def check_alarm_available():
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("Faultline execution requires the main thread for stage deadlines")
    if not hasattr(signal, "setitimer"):
        raise ValueError("Stage deadlines require Linux/WSL execution")
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise ValueError("An existing process alarm is active; run Faultline in a separate process")


@contextmanager
def alarm_until(stage, end, scope):
    """Restore the caller's handler on success, cancellation, and deadline expiry."""
    check_alarm_available()

    def expire(_signum, _frame):
        raise DeadlineExceeded(stage, scope)

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded(stage, scope)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        yield
        # Covers trusted code that returned only after delaying signal delivery.
        if time.monotonic() >= end:
            raise DeadlineExceeded(stage, scope)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class RunDeadlines:
    def __init__(self, limits):
        check_alarm_available()
        self.limits = limits
        self.started = time.monotonic()
        self.total_end = self.started + limits.total_ms / 1000
        self.work_end = self.total_end - limits.cleanup_ms / 1000
        self.stages = []

    @contextmanager
    def until(self, stage, end, scope):
        started = time.monotonic()
        record = {
            "stage": stage,
            "started_ms": round((started - self.started) * 1000),
            "allowance_ms": max(0, round((end - started) * 1000)),
            "scope": scope,
            "expired": False,
        }
        self.stages.append(record)
        try:
            with alarm_until(stage, end, scope):
                yield
        except DeadlineExceeded:
            record["expired"] = True
            raise
        finally:
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000)

    def stage(self, name, milliseconds, *, started=None):
        stage_end = (time.monotonic() if started is None else started) + milliseconds / 1000
        return self.until(
            name,
            min(stage_end, self.work_end),
            "total_work" if self.work_end <= stage_end else "stage",
        )

    def cleanup_windows(self):
        end = min(time.monotonic() + self.limits.cleanup_ms / 1000, self.total_end)
        # This time belongs to cleanup, but cannot be consumed by Docker deletion.
        # A timeout still needs a chance to persist unresolved IDs and the error report.
        reporting_reserve = min(1000, self.limits.cleanup_ms / 4) / 1000
        return end - reporting_reserve, end
