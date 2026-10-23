# Fault semantics

Fault semantics version: 1. Each fault is one-shot, belongs to a single run, and refers to symbolic targets resolved from that run's environment manifest. The engine MUST log requested, eligible, applied, healed, skipped, and failed dispositions separately.

Every application record includes fault ID/kind/version, resolved owned resource IDs, matching trigger event, actual start/end, controller identity, parameters, acknowledgment, and error if any. `applied` requires a kind-specific positive acknowledgment. A missing acknowledgment, failed admin call, or vanished target is a harness error. A trigger that never occurs is `not_triggered`, not a controller error.

## Controlled response suppression: `response_suppress`

Target: one HTTP route. Trigger: `upstream_response_complete` for one explicit operation/attempt and successful status. No offset trigger is permitted. There are no additional parameters.

The worker's transport wrapper sends the request to the real AUT and reads a complete response within the attempt deadline. If the rule matches, it durably records the response witness, acknowledges suppression, discards the response from the client policy's view, and waits until the original attempt deadline. It then emits client outcome `timeout` with cause `injected_response_suppression`. A response arriving after the deadline cannot trigger this fault. A required but unmatched status prevents PASS.

The response witness includes status and a sanitized, bounded body or hash; it is available to the investigator and response-contract verifier but MUST NOT be fed into the simulated client's retry decision. A retry happens only under the declared policy. A first-attempt response that never arrives is an ordinary timeout, not evidence that suppression was applied.

This fault guarantees a controlled divergence between received upstream evidence and client-visible outcome. It does not sever a TCP connection or emulate a kernel packet loss. It does not independently prove database commit; the final authoritative snapshot does. The bundled backend contract promises successful responses only after commit, and validation tests that promise.

Milestone 1 uses a 1000 ms attempt deadline and 100 ms retry backoff. There are no magic sleeps intended to guess a database commit boundary.

## Database response latency: `proxy_latency`

Target: a declared AUT-to-PostgreSQL proxy binding. Trigger: offset or `attempt_dispatched`/`client_outcome`. Required parameters are `latency_ms` (1–30000), `direction` (`upstream` or `downstream`), and `duration_ms` (1–60000). The mapping is explicit: upstream is AUT to database; downstream is database to AUT. Jitter is zero in v1.

The worker installs one run-owned Toxiproxy latency toxic, reads back effective configuration, and records acknowledgment. Duration starts at acknowledgment. Healing removes that exact toxic and confirms removal. Applied means the toxic was installed; it does not prove a particular query traversed it. A report MUST distinguish configured impairment from observed request impact.

The reference scenario starts the latency before releasing its workload barrier so exposure is known at the connection path level. If a scenario needs a specific SQL statement to be delayed, it needs a future statement-aware or instrumented capability; Toxiproxy cannot identify SQL transaction boundaries.

## Database connection interruption: `proxy_disconnect`

Target: the same dependency binding. Trigger: offset or `attempt_dispatched`/`client_outcome`. Required parameter is `duration_ms` (1–60000).

Disable the dedicated proxy, verify disabled state, and require that established connections are closed and new connections cannot pass for the duration. Re-enable it and verify readiness before observation. Acceptance must demonstrate both existing-connection interruption and new-connection refusal on the pinned Toxiproxy version. If the backend does not provide these semantics, implementation MUST change the supported mechanism/specification instead of silently substituting latency.

No database data is deleted. A disconnected transaction may have committed or rolled back; classify from authoritative state after recovery, not from the connection error. Connection pooling and application reconnect behavior are part of the experiment.

## Application crash/restart: `process_restart`

Target: one owned service with role `application`; worker, observer, proxy, coordinator, and database are prohibited targets. Trigger: offset, `attempt_dispatched`, `client_outcome`, or the capability-gated checkpoint below. Required parameter is `downtime_ms` (0–60000).

The coordinator sends SIGKILL to the application container's main process, confirms the container stopped, waits the declared downtime, then starts the same container with the same configuration and run-owned volumes. Engine restart policy is disabled so there is exactly one controller. `applied` requires a confirmed kill; `healed` requires restart and readiness. Record actual downtime and image identity. This is a process crash, not host failure, disk failure, database crash, or power-loss durability test.

The total fault/deadline budget must allow recovery; restarting after the run ends does not make a previously incomplete experiment pass. A failure to start or heal is ERROR while preserving any independently proven violation.

## Explicit checkpoint extension

Only adapters declaring checkpoint capability may use an event trigger with `event: adapter_checkpoint`, `checkpoint: transfer.debit_committed`, and an operation/attempt binding. This is the sole v1 extension to the general event vocabulary. It is allowed only for `process_restart` and the transfer profile.

A checkpoint must bind to an admitted attempt and be acknowledged through the control protocol after the debit commit is durable. The instrumented fixture exposes its checkpoint state on a dedicated test-only internal HTTP route reachable by the worker on the client network; the worker forwards the structured acknowledgment to the coordinator. This does not give the AUT access to the control network or Docker. The handler is paused until the fault controller acts or the attempt/checkpoint deadline expires. Correct atomic variants cannot expose a partial debit commit; they declare this checkpoint unsupported and use other restart schedules. The verifier still examines raw state independently; a checkpoint message alone never establishes a monetary violation.

## Composition and ordering

The planner MUST reject overlapping proxy faults on the same binding. If overlap cannot be ruled out statically, execution MUST serialize them only when the plan explicitly expresses an ordering; otherwise validation rejects the combination. v1 does not merge toxics or resolve conflicting settings automatically.

At most one lifecycle fault may be active per application service. Response suppression applies to one matched attempt only. Compatible faults on distinct targets may overlap; any cross-controller ordering required by the scenario must be established by acknowledged control events, not approximate wall-clock timestamps.

For offset/event-driven proxy faults, acknowledgment is asynchronous relative to a triggering request. `attempt_dispatched` means the request is admitted, not that the impairment necessarily affected it. Plans requiring the fault to be active before a request MUST use an explicit scheduler fault gate: an operation may declare `after_faults: [fault_id]`, which requires those faults' application acknowledgments before admission. This optional operation field is part of schema version 1. Fault-gate dependencies participate in cycle detection; a fault cannot wait on an attempt that is gated by that fault.

All supported faults MUST be healed before final observation. Healing is attempted after timeout, cancellation, and failure as well as success. Missed required triggers or insufficient fault coverage yield INCONCLUSIVE absent a conclusive violation/error; they never become PASS. Faults explicitly marked `required: false` may remain unapplied, with that limitation visible in the report.
