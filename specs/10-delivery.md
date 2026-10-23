# Delivery and acceptance

This is an implementation sequence, not an instruction to implement now. Each milestone must be demonstrable before expanding the fault catalog or UI. Time estimates from the proposal are planning assumptions, not commitments; usable-tool integration, evidence integrity, and safety will likely dominate the work.

## Milestone 1: one trustworthy failure

Deliver the minimal Python CLI/core, disposable Linux Docker environment, FastAPI order API, PostgreSQL authority, standard fixture, unsafe and correct-A variants, response suppression, one retry, required order invariants, and JSON/Markdown evidence. The CLI may expose only the commands needed for this gate; the React viewer is not on the critical path.

Gate requirements:

- **AT-001 — Normal control:** Twenty fresh no-fault runs of the ordinary one-purchase scenario PASS for both unsafe and correct-A variants, with independently reconciled state.
- **AT-002 — Lost response:** Twenty fresh runs of the suppression scenario show the required fault applied, a client-visible timeout, the declared retry, and duplicate debit FAIL for the unsafe variant. Twenty fresh runs against correct-A PASS with one charge. Any ERROR/INCONCLUSIVE blocks this gate until explained and rerun; never count it as a correct negative.
- **AT-003 — Ambiguous completion:** A final client timeout with one authoritative committed purchase is classified by the invariants, not automatically as failure. A never-completed expected purchase cannot obtain PASS by merely doing no harm.
- **AT-004 — Wrong fault boundary:** If no successful upstream response arrives, or the observed status does not match, response suppression is not marked applied. With no other failure, the required-fault scenario is INCONCLUSIVE.
- **AT-005 — Independent authority:** Altered/misleading successful responses, duplicate rows, and debit-without-order state are caught through authoritative evidence. Hand-audited expected ledger totals agree with the verifier.
- **AT-006 — Isolation:** All resources are run-owned, no AUT ports are published, and unrelated containers/networks/volumes remain unchanged before and after success, failure, and cancellation.

Demonstration output must include actual expected/observed integer amounts, row IDs, attempt identities, and reproducible plan/artifact paths. A recorded narration or screenshot alone does not satisfy the gate.

## Milestone 2: usable experiment engine

Add the full strict project/scenario contract, generator materialization, barriers and dependencies, bounded concurrency, all v1 fault kinds, adapter registration, robust recording, lifecycle deadlines, cleanup/recovery, and external integration documentation.

- **AT-007 — Plan stability:** The same scenario/seed and pinned generator version expand to identical canonical plans across repeated processes. Changing asynchronous completion order does not alter generated values. Replay uses saved materialized values.
- **AT-008 — Scheduler semantics:** Test conditional retries, no hidden retries, barrier release/admission, maximum in-flight count, fault gates, cycle rejection, and `not_needed` attempts. Request content and idempotency key remain unchanged across retries.
- **AT-009 — Fault contracts:** Demonstrate downstream database latency, established/new connection interruption, and application SIGKILL/restart. Verify acknowledgments, actual timings, healing, path routing, and distinct client-visible outcomes. No test may infer “fault applied” only from a failed HTTP request.
- **AT-010 — Quiescence:** A handler that continues after client timeout cannot produce a premature PASS. Observation waits for declared completion or yields UNKNOWN/INCONCLUSIVE. A crashed handler cannot leave an unbounded wait; session termination and stale registry cleanup are verified.
- **AT-011 — Unsafe input rejection:** Reject external URLs, malformed/duplicate YAML fields, unsupported capabilities, impossible fault gates, host mounts, privileged services, nonlocal Docker, and attempts to target a resource from another run before any unsafe action.
- **AT-012 — Harness failures:** Inject readiness failure, adapter exception, worker stream loss, invalid snapshot, disk exhaustion, Ctrl+C, and cleanup denial. Preserve partial evidence, follow the specified verdict/exit precedence, and clean owned resources where possible.
- **AT-013 — External adapter:** Integrate a separately packaged trusted HTTP/PostgreSQL application using the documented public adapter contract without changing core source. Expose capability errors clearly; no hardcoded bundled-image checks.

Run a bounded scenario/variant matrix with each database fault individually before combining faults. Combined-fault coverage is demonstrated only for supported nonconflicting compositions. An unsupported combination is a validation failure, not an experiment.

## Milestone 3: replay, reduction, and investigation

Add strict replay/comparison, group persistence, dependency-aware reduction, the read-only report API, and React viewer.

- **AT-014 — Replay fidelity:** Exact materialized plan, fixture, and image identities survive replay. Missing images, modified adapters, and unsupported contract versions are rejected clearly. Explicit variant comparisons show their manifest differences. Every trial starts from the baseline.
- **AT-015 — Honest frequency:** A controlled mixture of matching FAIL, unrelated FAIL, PASS, INCONCLUSIVE, ERROR, and interrupted trials produces the exact counts/denominators specified. Zero denominators are handled. No ERROR trial becomes a successful reproduction merely because it also contains a violation.
- **AT-016 — Smaller reproduction:** Begin with a valid scenario containing at least 16 operations, including a reproducible duplicate-debit failure and unrelated valid purchases. Reduce logical operation count by at least 75%, retain the selected signature, and satisfy final five-run validation. Report original/candidate counts and all trial outcomes. This is an acceptance target, not a result already achieved.
- **AT-017 — Reduction integrity:** Test operation/fault/retry dependencies, barrier deletion, unrelated failure signatures, unstable baselines, no possible reduction, budget termination, and incomplete final validation. Original bundles remain immutable.
- **AT-018 — Artifact robustness:** Round-trip supported artifacts; detect corruption, missing required events, duplicate conflicting event IDs, truncated last lines, unsupported versions, and path traversal. A viewer cannot execute an adapter or artifact content.
- **AT-019 — Viewer investigation:** From the run list, reach a violation, its attempts, applied fault, and exact ledger rows using mouse and keyboard. Distinguish upstream response from client timeout, plan from observation, ERROR from FAIL, and unreproduced from verified reductions. Test empty/loading/corrupt/incomplete states and narrow layouts.
- **AT-020 — Local web boundary:** Verify token enforcement, Host/Origin checks, disabled mutation/CORS, no external resources, output escaping, and safe artifact downloads. Requests cannot escape the chosen root or bind the server externally.

The reduction acceptance fixture `reduction_market` extends `standard_wallet` with 19 independent accounts `buyer-01` through `buyer-19`, each with 1000000 paise, and `sku-noise` priced at 10000 paise with stock 100. The failing purchase still uses `buyer-a`/`sku-a`. Noise operations are real successful purchases with unique identities, so removing them preserves input validity without changing the target fixture.

## Milestone 4: calibrated release and CI

Complete correct-B and the transfer application, known defect fixtures, CI documentation, portable reports, packaged frontend assets, platform/resource measurement, and reviewer setup instructions. The public release is gated on all earlier requirements, not only the demo.

Required defect corpus:

1. Missing idempotency: suppress a committed response, retry, and find duplicate debits by logical purchase ID.
2. Check-then-insert idempotency race: barrier-release identical purchases, use bounded concurrent repetitions, and find duplicate committed effects. A uniqueness constraint that prevents the defect is not a valid broken fixture.
3. Non-atomic order side effects: interruption/restart exposes an orphan debit or order. Report observed reproduction frequency; do not claim a precise SQL boundary from a generic proxy fault.
4. Lost balance update: concurrent independent purchases from one account both persist ledger effects but overwrite the balance update; conservation detects the mismatch.
5. Idempotency payload mismatch: reuse a key for a conflicting payload and expose an incorrect success/new effect or missing required rejection.
6. Missing confirmation intent: create a successful order with no outbox record; the outbox invariant catches it even if HTTP and wallet checks pass.
7. Non-atomic transfer: kill the instrumented transfer application at the declared post-debit checkpoint and detect an unmatched debit/credit through the independent transfer adapter.

At least five distinct defect types MUST be detected with authoritative evidence, including missing idempotency, one concurrency defect, payload mismatch, and the transfer defect. All seven are the planned corpus; if fewer than seven are implemented, the release documentation must list omissions and must not count multiple fixtures of one defect as separate types. Correct variants must be tested against all applicable scenarios; unsupported instrumented checkpoints are explicitly excluded from comparisons and do not count as passing tests.

- **AT-021 — Corpus discrimination:** Execute at least twenty fresh trials for each selected defect/scenario pair and record all outcomes, detection counts, and coverage. Each required defect must produce a conclusive matching violation at least once. Zero detections blocks its inclusion in the demonstrated corpus, even if a source-code review identifies the bug. Record concurrency flakiness openly. No global detection-rate claim can be inferred from this small selected corpus.
- **AT-022 — Correct implementations:** Correct-A and independently implemented correct-B each PASS twenty fresh repetitions of every applicable release scenario, including no-fault controls, duplicates, payload conflicts, concurrency, response suppression, proxy failures, and uninstrumented restarts. Scenario progress requirements must be feasible within their declared fault/recovery bounds. Every false alarm or unknown outcome is investigated, fixed or explained with a documented unsupported scope, and rerun before claiming the gate met. Do not remove a real failing supported scenario simply to make the matrix green.
- **AT-023 — Independent domain:** The transfer application's contract and invariants run through the same engine without order-specific core code. Correct transfer fixtures preserve balances/atomicity; the non-atomic variant produces a real violation.
- **AT-024 — Regression:** Preserve the unsafe failure plan, compare it against a corrected image, show the before/after state and all manifest differences, and run the corrected scenario in CI. The fixed run uses the same business inputs, property versions, and fault rules.
- **AT-025 — Reviewer usability:** On a fresh supported Linux/WSL2 environment, documented install/build/run/report commands reproduce the demo without hidden local files, Node for viewing, production credentials, or manual database changes. First image build/pull requirements and approximate disk/time costs are stated.
- **AT-026 — Resource measurement:** Run the reference scenario within the four-core/8 GiB host budget, record peak container memory and wall time, and check zero leaked resources after twenty serial runs. The target is a warm-image first scenario within 60 seconds and a small PR matrix within 10 minutes on the documented reference machine; report actual measurements and adjust scope/budgets explicitly if targets are missed.

## CI behavior

A GitHub Actions workflow runs on pull requests and manual dispatch on Linux with Docker. It pins actions to commit SHAs, grants only required read permissions, uses no production secrets, and does not use `pull_request_target` to run untrusted pull-request code with privileges. Job timeout bounds total work and leaves time for cleanup/artifact upload.

The ordinary PR job runs correct variants and gates on overall PASS. A separate calibration job runs known-bad variants and asserts their expected signature, sufficient evidence, and absence of harness errors; it does not mistake the CLI's expected exit 1 for a broken CI setup or blanket-ignore nonzero exits. INCONCLUSIVE and ERROR fail both kinds of job. Reduced regressions join the ordinary suite after the application fix.

Always upload available JSON/Markdown/history/snapshots and group summaries even when execution fails, with a default retention of seven days and documented sensitivity. Optional logs are bounded/sanitized. Workflow summaries link the primary violation and artifacts. Automatic PR comments, external report hosting, and watching third-party repositories are not required.

## Requirement traceability

- FL-001: AT-006, AT-012, AT-014, AT-026.
- FL-002: AT-013, AT-023, AT-025.
- FL-003: AT-002, AT-003, AT-008.
- FL-004: AT-004, AT-009.
- FL-005: AT-005, AT-010, AT-021, AT-022.
- FL-006: AT-003, AT-004, AT-012, AT-015.
- FL-007: AT-012, AT-018.
- FL-008: AT-007, AT-014, AT-015, AT-024.
- FL-009: AT-016, AT-017.
- FL-010: AT-006, AT-011, AT-020.
- FL-011: AT-018, AT-019, AT-020.
- FL-012: AT-012, AT-015, AT-024 and the CI behavior contract.
- FL-013: AT-005, AT-021, AT-022, AT-023.
- FL-014: AT-008, AT-012, AT-017, AT-026.
- FL-015: AT-007, AT-014, AT-018.

## Definition of done

All MUST requirements for the release are satisfied, acceptance evidence is retained, supported/unsupported cases are published, the documentation matches actual command/schema versions, and no implementation-only assumptions are necessary to reproduce a result. A reviewer can inspect an invariant, follow its source records, rerun its scenario, understand frequency, and see the fixed application pass without trusting a dashboard's assertion.
