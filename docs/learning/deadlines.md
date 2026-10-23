# Time budgets: leave time to clean the laboratory

## The simple explanation

Imagine renting a laboratory for five minutes. If you spend all five minutes on the experiment, you have no time to put the equipment away. Faultline therefore has an overall timer, timers for individual jobs, and time set aside for cleanup.

A request timeout answers “how long will this client wait for one reply?” A stage deadline answers “how long may this whole part of the experiment take?” A run budget answers “when must this experiment, including cleanup and reporting, end?” These are different clocks with different consequences.

## The supported settings

These fields belong inside the scenario's existing `limits` object. All values are integer milliseconds; 1,000 ms is one second.

| Field | Default | What it covers |
| --- | --- | --- |
| `provisioning_ms` | 60,000 | Artifact initialization, Docker creation, fixture setup, baseline reading/validation, and application/worker readiness |
| `execution_ms` | 60,000 | Sending the workload, recording its events, response suppression, retries, and backoffs |
| `drain_ms` | 10,000 | Asking the application to finish and exit gracefully |
| `observation_ms` | 10,000 | Recording quiescence evidence and collecting/persisting the final snapshot |
| `verification_ms` | 10,000 | Evaluating invariants and recording their results |
| `cleanup_ms` | 30,000 | Removing resources and finalizing the report |
| `total_ms` | 300,000 | The run from artifact initialization through finalization |

Each individual stage limit is positive and at most 300,000 ms. `total_ms` is positive and at most 900,000 ms, and must be strictly greater than `cleanup_ms`. Booleans and strings are not accepted as integer durations.

The default execution budget used to be 30 seconds in the first implementation. It is now 60 seconds, matching the release specification. A scenario explicitly choosing 30 seconds retains that value. All effective defaults are saved in the plan, so this change affects plan hashes for scenarios that omit the value. Historical replay is still not implemented.

The total run budget begins **after validation/preflight and lock acquisition**, immediately before creating the bundle. Image builds/pulls, command parsing, preflight, and the entire repeat group are not included. Each repeated run receives its own budget. `clean --run` and `clean --stale` are separate recovery commands; their whole-command deadline remains deferred.

Adapter resolution now has its own fixed ten-second allowance before Docker and locking. It covers discovery, provenance, imports, construction, description, and validation, for bundled and installed adapters. Expiry produces `adapter_preflight_timeout` and exit 4 without creating a run bundle. This separate allowance does not bound the whole preflight command or repeat group. See the [installed-adapter guide](installed-adapters.md).

Internal request, helper, readiness, and database timeouts still exist. Stage limits are ceilings, not a promise that a helper will wait that long. For example, a ten-second helper timeout can fail before a longer provisioning deadline.

## Work through the arithmetic

Suppose `total_ms` is 90,000 and `cleanup_ms` is 30,000. The latest ordinary work can continue is 60 seconds after the run starts. This leaves 30 seconds for cleanup and reporting.

Suppose provisioning uses 20 seconds. Execution may request 60 seconds, but only 40 seconds remain before the reserved cleanup window. Its effective deadline is therefore shortened. Every later work stage is subject to the same absolute boundary; starting a new stage does not reset the total clock.

The sum of stage maxima can exceed the total budget. They are individual upper limits, not promises that every stage gets its maximum. A shorter total budget intentionally clips them. Static validation still requires the configured execution budget to cover the declared request timeouts and backoffs, but overhead or a shorter remaining total budget can stop actual execution earlier.

If an early stage fails after just two seconds, cleanup begins immediately. It does not wait until the end of the work window. Cleanup gets at most its configured duration, further limited by the remaining total budget.

## Cleanup has a reporting reserve too

Some cleanup time is set aside for writing the result:

```text
reporting reserve = smaller of 1,000 ms and one quarter of cleanup_ms
resource-deletion deadline = cleanup end minus reporting reserve
```

With the default 30-second cleanup budget, Docker deletion can use at most 29 seconds. At least one second is reserved for reporting if the coordinator can respond to the alarm promptly. If deletion finishes early, reporting can use the unused cleanup time as well. The finalization deadline is never later than either the cleanup end or total run end.

This reserve does not guarantee a successful disk write. A very small budget, a full disk, or a stalled filesystem can leave a partial bundle. Faultline returns an operational error instead of claiming that an incomplete report is a trustworthy PASS.

## How the timer interrupts work

[deadlines.py](../../src/faultline/deadlines.py) uses the Linux `ITIMER_REAL` timer and `SIGALRM` signal. The main Python thread receives the alarm and raises a dedicated `DeadlineExceeded` control-flow exception. Each stage installs the alarm, then disables it and restores the previous handler when the stage exits, including on errors and cancellation.

The exception inherits directly from `BaseException`. This is deliberate: ordinary libraries often catch `Exception` or `TimeoutError` to turn a network failure into a retry. A coordinator deadline must reach the engine; it must not be mistaken for an HTTP timeout and silently retried.

The engine requires the main thread and refuses to replace an already-active process alarm. An embedding application that owns a timer or needs threaded orchestration should run the CLI in a separate process. This is a bounded Linux CLI design, not a general signal-sharing framework for arbitrary plugins.

We chose an alarm because checking the clock only between stages cannot interrupt a blocked stage. A background thread with a wait timeout also does not stop the work running in that thread. A separate supervisor process is a possible later design for stronger isolation; it needs explicit ownership handoff and evidence coordination.

Python signals are not a hard real-time operating-system guarantee. A native extension that does not return control, an uninterruptible kernel operation, a malicious signal handler, or an unresponsive Docker daemon can delay progress. The project supports trusted bundled code. Timing measurements and unresolved resources make limits visible rather than promising perfect interruption of every possible operation.

## What a deadline does to the verdict

| Expired stage | What Faultline does |
| --- | --- |
| Provisioning | Stops setup, attempts bounded cleanup, reports ERROR when finalization is possible |
| Execution | Stops new requests; retains completed attempt/fault evidence; tries drain, observation, and verification if ordinary-work time remains |
| Drain | Marks graceful completion uncertain and tries observation; uncertain final state cannot produce a full PASS |
| Observation | Reports ERROR because a complete authoritative snapshot was not obtained |
| Verification | Reports ERROR because the judge did not finish; timeout is not a business-rule violation |
| Cleanup | Reports ERROR, retains already-computed findings and unresolved recorded resource IDs, and leaves scoped recovery available |
| Initialization or finalization without enough time | Returns an operational error; the bundle may be incomplete and is preserved for investigation |

An execution deadline alone does not prove the business failed. The server may have committed before the client stopped waiting. When later evidence is adequate, a definite business violation can still be FAIL. Otherwise unfinished workload or uncertain state yields INCONCLUSIVE. If the total work allowance has already expired, later observation cannot start and the run becomes ERROR with the relevant deadline diagnostic.

A cleanup error takes precedence over otherwise passing invariants. The report retains those invariant results so the reader can see “business checks passed, but teardown failed,” rather than losing useful evidence.

## What gets saved

Each deadline diagnostic identifies `stage`, `scope`, and a readable message. `scope` distinguishes a stage limit from the shared total-work boundary or cleanup/finalization window.

`report.json` includes `timing` with the total and cleanup budgets, elapsed time before report assembly, and stage start offsets, effective allowances, elapsed durations, and expiry flags. Finalization's completed duration is intentionally absent: the report is written during that stage, so its ending duration is not yet known. These values are coordinator measurements, not cross-container clock comparisons.

Partial completed attempts and applied faults remain in the report if execution is interrupted. An attempt still in progress may have only its dispatch and upstream events in the history; a stage deadline does not invent a terminal client response. Raw history is preserved even when finalization cannot complete.

## What happens to work inside containers?

Interrupting the coordinator does not cancel a PostgreSQL transaction or magically kill an exec helper. A timed-out request can continue on the server. That is why the engine still needs drain and observation.

When an experiment must abort, cleanup removes the owned application/helper containers. Removing the container terminates its remaining processes. If cleanup cannot finish, the report records unresolved IDs and the operator can use scoped recovery. The engine does not claim that a failed cleanup has stopped every remaining writer.

The existing gap between Docker creating a resource and the coordinator persisting its ID also applies to a deadline that lands in that interval. No broad label/name sweep is introduced to guess ownership afterward.

## Tests to read

[test_deadlines.py](../../tests/test_deadlines.py) covers validation, real main-thread alarms, interrupted blocking waits, CPU-bound grading, existing-timer refusal, main-thread requirements, total-budget clipping, partial execution evidence, uncertain drain, cleanup reports, and finalization failure.

[test_integration.py](../../tests/test_integration.py) includes a real observer helper sleeping inside a container. The observation alarm must cut short the wait and cleanup must remove the helper. A separate case deliberately stalls cleanup while real resources exist; the run must report ERROR, keep the unresolved IDs and completed findings, and allow a later `clean --run` to remove the leftovers without rewriting the original report.

See [implementation status](../implementation-status.md) for current support and the acceptance documents for measured outcomes. A configured duration is a target; only an executed test is a measurement.
