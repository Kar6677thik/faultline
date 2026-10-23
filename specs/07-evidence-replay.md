# History, replay, and reduction

## Artifact bundle

Default location: `.faultline/runs/<run-uuid>/`. A bundle contains these specified artifacts when their stages have been reached:

- `manifest.json`: bundle/schema versions, run and parent/group IDs, origin, times, lifecycle stage, engine/adapter versions and hashes, image digests, platform, fixture identity, effective budgets, cleanup ownership, and artifact inventory.
- `plan.json`: canonical expanded plan, independent of the run UUID.
- `history.ndjson`: append-only event stream. Each complete line is one JSON event. The final report identifies its last acknowledged sequence and content hash.
- `snapshots/baseline.json` and `snapshots/final.json`: canonical authoritative state with completeness and transaction metadata; intermediate snapshots are optional and named explicitly.
- `report.json`: verdict, invariant results, coverage, diagnostics, artifact hashes, signature(s), cleanup status, and schema versions.
- `report.md`: portable human-readable summary derived from the JSON report, never a separate source of truth.
- `logs/`: bounded sanitized service logs, when available. Missing optional logs must not invalidate otherwise sufficient evidence.

Write final JSON and Markdown using temporary files and atomic rename. The manifest inventory hashes completed payload artifacts; it does not hash itself or create a circular report/manifest hash dependency. The report records hashes of evidence inputs; the manifest records the completed report hash. Hashes detect accidental corruption, not malicious authorship or authenticity.

A crash may leave complete history lines and a partial last line. Recovery preserves the original file and labels the run ERROR/interrupted. It may build a diagnostic view from intact lines but MUST NOT reinterpret the run as completed. Disk-full or artifact-capacity exhaustion stops new work, attempts cleanup, and produces ERROR if a final report can still be written. A reserved small diagnostic file/budget SHOULD support reporting the failure.

## Event envelope

Required fields: `event_schema_version`, `run_id`, `event_id`, `sequence`, `source`, `source_sequence`, `source_monotonic_ns`, `received_monotonic_ns`, `wall_time`, `type`, `payload`, and optional references `operation_id`, `attempt_id`, `fault_id`, `caused_by_event_ids`. The coordinator allocates contiguous `sequence` values on durable append. Each source sequence is monotonic; duplicate source events are deduplicated by event ID, with conflicting duplicate content treated as ERROR.

Payloads are discriminated by event type and validated. Minimum types cover lifecycle transitions, fixture readiness, baseline capture, operation eligibility, barrier release, attempt dispatch, upstream response completion, client outcome, retry eligibility/dispatch, attempt cancellation, fault eligibility/application/healing, adapter checkpoint, quiescence, snapshot capture, invariant evaluation, resource cleanup, and diagnostics.

Each attempt records request identity/body hash, actual dispatch time, visible outcome (`http_response`, `timeout`, `connection_error`, `invalid_response`, `cancelled`, or `not_dispatched`), status if present, duration, and response evidence. Unneeded potential retries are explicitly `not_needed` in plan reconciliation. A client timeout is a client observation with unknown server completion until state is examined.

Critical events such as response suppression and proxy application MUST be acknowledged after durable append before the associated next action is admitted. Bounded queues MUST apply backpressure; dropping evidence silently is prohibited. Backpressure and recording overhead are part of the measured harness behavior and MUST be disclosed in timing data.

## Serialization and compatibility

Bundle, plan, history, report, snapshot, adapter, and fault semantics versions are separate fields, initially 1. v1 accepts its known major versions only. Unknown fields in persisted artifacts may be preserved for forward inspection but MUST NOT be used for execution until their versions are supported. This differs from strict rejection of unknown input configuration fields.

Unsupported bundles may be shown as metadata-only with an explicit compatibility error. The viewer MUST NOT execute migrations or adapter code. A future migration must create a separate derived bundle with provenance, leaving the original immutable. Replay rejects mismatched engine/adapter/fault contracts or unavailable image digests; no silent repinning to `latest` is allowed.

## Replay and comparison

Replay loads the saved materialized plan, exact fixture identity, pinned images, and contract versions. It validates the environment, provisions fresh resources, and executes the normal lifecycle. It preserves conditional retry rules and intended schedule; different observed outcomes may change which conditional attempts run. The new bundle links to the source run and its plan hash.

Strict replay requires identical digests for every application/dependency/worker image, adapter content/version, engine package version/hash, invariant versions, and engine/fault semantics versions. The supported platform tuple (OS family, CPU architecture, kernel release, Docker Engine version) and effective resource limits must also match; differences require explicit comparison mode. Machine name, run ID, absolute workspace path, and wall time may differ. Even strict replay does not promise identical operating-system or transaction scheduling.

A variant/image or engine change is a **comparison run**, requested explicitly, with a manifest diff. It may test the same plan against a corrected application but MUST NOT be labeled an exact replay. The UI shows scenario identity and environment differences side by side.

Repeats default to 5, execute serially, and always reset the environment. A repeat group records total scheduled, started, completed, matching-signature FAIL, other FAIL, PASS, INCONCLUSIVE, ERROR, and interrupted/unstarted counts. It reports `matching_failures / started_runs` and separately `matching_failures / conclusive_runs`, where conclusive means PASS or FAIL with no harness error. Neither fraction excludes inconvenient attempts without disclosure. A zero denominator is `not_available`. These are descriptive observations, not an estimate of all possible schedules.

A newly generated run may be replayed even if the source was PASS, provided its plan/environment are complete; reproduction matching applies only when a target signature exists. Comparison/repeat summaries MUST not hide errors behind an aggregate pass rate.

## Reduction

Inputs are a complete failing bundle, a selected signature (required when multiple exist), a fixed execution environment identity, and budget. The default budget is 50 trial runs or 10 minutes wall time, whichever comes first; a user may raise it up to 500 trials or 60 minutes. Time includes fresh provisioning and cleanup. Every started trial consumes the run budget even if inconclusive or erroneous.

Before deleting anything, execute five fresh baseline reproductions. The default reducer requires the selected signature in at least four of five runs, with no harness errors. If it is less stable, preserve all counts and return `baseline_unstable`, without claiming reduction succeeded. A future statistical reducer is out of scope.

The reduction units are explicit logical operations (with their attempts), fault rules, and optional retry attempts after the first. The algorithm first tries partitions of operations, then faults, then reducible retry suffixes. It uses dependency-aware delta debugging: remove a partition, compute dependent-node removal closure, validate, rerun, and retain the change only if the same target signature satisfies the acceptance rule. Invalid candidates consume planning time but no trial execution budget.

Dependencies include operation order, barriers, fault gates, response-trigger references, retry prerequisites, and adapter-declared semantic prerequisites. Removing an operation removes faults whose trigger references it. Removing a fault also removes operations gated by its application unless the adapter declares a valid equivalent ungated plan; v1 does not rewrite such gates automatically. A barrier loses removed members and disappears if none remain. No dangling references or changed business payloads are permitted.

For each candidate, execute up to three fresh trials and accept only three matching-signature FAIL outcomes with no harness error. Early rejection is allowed after the first nonmatching outcome; record the actual denominator. A matching invariant finding inside an overall ERROR is not an accepted reproduction. Candidate selection is deterministic from the materialized plan, deletion order, and previous observed results; execution outcomes are not deterministic.

The objective is lexicographic: fewer logical operations, then fewer faults, then fewer potential attempts. Changes that do not strictly improve this tuple are rejected. Timings, data values, fixture size, and request payloads are not minimized in v1. The algorithm does not promise a global minimum; with repetitions and budget limits it may not establish even a one-deletion minimum.

Reserve five trial slots and enough wall budget to validate the best accepted candidate. Final validation uses five fresh runs and reports the exact counts; at least four matching failures with no ERROR earn `reduction_verified`. If budget expires before validation or stability is lower, report `reduction_unverified` and keep the original plus best candidate. A candidate cannot become the “verified reduced reproduction” based only on its search trials. If no candidate improves the objective, return `no_smaller_reproduction` with the original baseline results.

All candidate plans, decisions, actual trials, signature matches, environment hashes, budget consumption, and termination reasons are persisted under a reduction group. Disk usage across the group is also bounded as defined in the safety specification. A report must not call a case “minimal”; use “smaller reproduction found” and state what was removed.
