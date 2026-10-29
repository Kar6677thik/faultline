# Implementation status: M2 complete, M3 in progress

This page describes executable behavior. The numbered files in `specs/` define the target v1 release. M2 is complete for the supported HTTP/PostgreSQL experiment engine; measured evidence and limits are recorded in [M2 acceptance results](m2-acceptance-results.md). The full v1 release is still pending.

## Milestones remaining

| Milestone | Status | Scope |
| --- | --- | --- |
| M1: one trustworthy failure | Demonstrated | Original unsafe/correct PostgreSQL purchase remains a regression |
| M2: usable experiment engine | Complete | Materialized plans, concurrency, fault controllers, public adapter integration, lifecycle evidence |
| M3: replay, reduction, investigation | In progress | Strict replay, comparison, and repeat groups implemented; reduction, read-only API and React pending |
| M4: calibrated release and CI | Pending | Independent correct-B, transfer domain, defect corpus, CI/release packaging and resource measurements |

## Implemented behavior

- Python CLI: doctor, validate, run, replay, compare, report, and scoped clean. Repetitions create fresh fixtures; they are not deterministic replay.
- Strict replay (`replay RUN`, default five trials) rebuilds the saved materialized plan and saved project configuration. It refuses before provisioning unless the plan hash, engine version, adapter package/version/content hash, and complete platform record (OS, architecture, kernel, Docker version, every image ID, project hash, variant) are identical. Comparison (`compare RUN --variant NAME`) runs the same plan against an explicitly chosen variant, records every difference in the group and each manifest, and rejects changed requests, invariant versions, or fixture identity. Neither reproduces OS or transaction scheduling.
- Repeat, replay, and comparison groups persist after every trial under `<output>/groups/`. They count scheduled, started, completed, interrupted, and unstarted trials; each verdict; and matching versus other failures for a target signature. Reproduction is reported per started and per conclusive trial, with `not_available` for zero denominators. ERROR and interrupted runs never count as reproductions. `report GROUP_ID` shows a group.
- Strict bounded JSON-compatible YAML, including duplicate/alias/unknown-field rejection. Generic HTTP/JSON requests remain confined to declared internal routes. Adapter validation checks domain-specific inputs and expectations.
- Up to 1000 operations, 5000 potential attempts, 32 fault rules, and concurrency 1-32, within project/scenario budgets. At most five attempts per operation. Explicit HTTP-status or transport retry policies, immutable retry bodies, unused-attempt records, dependency gates, and barriers.
- The bundled versioned `orders.purchases` and installed adapter generators expand before execution. Returned operations undergo request, graph, count, and project-budget checks. Plans save concrete inputs, body bytes/hashes, effective defaults, potential attempt IDs, fixture digest, and invariant versions. Three-process tests compare canonical output exactly for orders and the independent counter.
- Worker protocol 2 multiplexes bounded concurrent requests over one persistent Docker exec stream, with admission acknowledgments, correlation, bounded frames, and explicit harness errors on stream loss.
- Five owned services on four internal networks: application, PostgreSQL, Toxiproxy, worker, observer. The application cannot reach PostgreSQL directly or proxy administration. Admin binds to a control-only alias; no ports are published.
- Controlled response suppression, acknowledged proxy latency/disconnect, and owned-application SIGKILL/restart. Configuration read-back and healing are recorded; only acknowledged actions count toward coverage. One proxy fault per binding and one lifecycle fault per service avoid ambiguous same-target overlap. Compatible different-target compositions are tested separately from individual faults.
- Independent complete read-only PostgreSQL snapshots; seven order invariants per logical identity, shared-state reconciliation, orphan detection, positive completion, and explicit expected rejection checks. The verifier does not receive the environment or variant.
- An order in-flight registry records work before business mutation. Drain checks owned-process exit and database-session termination before clearing stale diagnostic rows. A handler continuing after client timeout and a crashed handler are covered by real integration tests. Public quiescence returns ready/pending/unknown with evidence under a drain deadline.
- Registered trusted Python adapters with exact package versions and bounded installed-file provenance. A separately built counter package/image uses its own HTTP route, PostgreSQL schema, fixture helper, and verifier without core changes. Offline reporting does not import the adapter.
- Five-role project manifests, locally resolved image IDs, declared routes, named/default variants, resource allocations, readiness settings, literal environment values or local secret references, constrained ephemeral writable paths, and project budget ceilings. No arbitrary URL, host mount, privileged service, remote Docker daemon, or user-selected foreign resource IDs.
- Durable plans/manifests/history/snapshots/JSON+Markdown reports and integrity checks. Stage/total budgets reserve cleanup/reporting time; interrupted runs retain partial evidence. Cleanup errors take precedence while retaining available findings.
- Recovery uses the original output-root lock, recorded immutable IDs, exact ownership labels, and prewritten creation intents. The inventory now supports five containers and four networks. Later cleanup persists recovered bindings separately and does not rewrite experiment artifacts.

## Supported boundaries and limitations

The bundled order fixture still has one account, one SKU, finite funds, and no broker/background worker. The counter example demonstrates domain onboarding; it is not M4's transfer application or independent correct-B implementation. Only adapter-advertised capabilities are accepted. A generator does not make an infeasible positive workload feasible.

The project model intentionally supports the five-role HTTP/PostgreSQL topology, not arbitrary service graphs. Images are trusted and must satisfy the documented UID, filesystem, dependency, and worker/proxy protocol contracts; a different tag is not proof of compatibility. Writable tmpfs paths are restricted to `/data` and `/cache`; host paths are never mounted. Resources remain bounded to 64-256 MiB, 100-1000 CPU millis, and 16-64 PIDs per service. The pinned PostgreSQL template uses UID/GID 70 for its disposable storage.

Request timeout is 50-30000 ms. Stage budgets are at most 300000 ms each; total is at most 900000 ms. The total timer starts after preflight and lock acquisition. Main-thread Linux alarms bound trusted code, not uninterruptible native/kernel operations. Entire repeat groups and standalone recovery do not yet have configurable whole-command deadlines. No power-loss durability guarantee is made for ephemeral PostgreSQL or filesystem evidence.

Ownership recovery refuses moved/copied bundles whose original output lock cannot be established. A missing unbound creation remains uncertain even when no object is currently visible. Binding records and cleanup receipts must be retained. Recovery never scans Docker for name-prefix matches or rebinds a known ID by name.

Third-party code is trusted, not sandboxed. Provenance records actual selected package/source bytes but does not attest every dependency, imported memory object, or container's Python environment. No production target option exists. Full v1 release work remains M3 and M4; React is not implemented.

See [the experiment-engine walkthrough](learning/experiment-engine.md), [external adapter tutorial](learning/external-adapters.md), and [decision log](learning/decisions.md) for source/test maps and design tradeoffs.

## Verification status

M3 Phase A validation on 2026-10-29: **315 unit tests passed**, with 40 Docker tests skipped; lint and format passed. After a runtime rebuild, the real replay/comparison case **passed in 120.61 seconds**. It produced 3/3 matching duplicate-debit replays with byte-identical plans and image IDs, and a 2/2 PASS comparison against the corrected variant whose only recorded differences were `variant` and `platform.variant`. See [M3 acceptance results](m3-acceptance-results.md). The full 40-case Docker regression was not rerun for this phase.

Latest M2 validation on 2026-10-24: **299 unit tests passed**, with 40 Docker tests explicitly skipped; lint and format checks passed. The full **38-case Docker/PostgreSQL regression passed in 827.82 seconds** before the last generator contract refinements. After those refinements and a runtime rebuild, **four focused Docker cases passed in 82.09 seconds**, including both external retry variants and two generated workloads. These are separate build/test phases, not a single 40-case pass on the final revision. The unsafe counter produced FAIL with total 14, the correct retry case PASS with total 7, generated counters PASS with total 186, and generated orders PASS with 600000 paise of debits. No owned containers or networks remained. [M2 acceptance results](m2-acceptance-results.md) maps AT-007 through AT-013 to source/tests, includes failed provisioning phases and fixes, and records exact runtime IDs.

Latest creation-recovery validation on 2026-10-24: **260 unit tests passed**, with 22 Docker tests explicitly skipped; the corrected full **22-case Docker/PostgreSQL suite passed in one invocation (310.91 seconds)**. Both real SIGKILL creation-window cases recovered IDs absent from their original manifests, preserved all original files, and passed repeated cleanup. The first integration invocation exposed a SIGTERM/event-sequence race after 14 passing cases; the fix and new artifact tests preceded the rebuilt runtime and full corrected pass. No owned resources remained. See [creation-recovery acceptance results](creation-recovery-acceptance-results.md) for separate phase measurements. This does not claim deterministic replay or machine-crash durability.

The earlier installed-adapter validation on 2026-10-23 had **237 unit tests passed**, with 20 Docker tests explicitly skipped; the full **20-case Docker/PostgreSQL suite passed in one invocation (347.07 seconds)** after rebuilding the runtime. A separate installed wheel produced duplicate-debit FAIL, corrected PASS, and incomplete-verifier ERROR; report and scoped cleanup worked without the package or project file. No owned resources remained. Measurements and evidence are in [installed-adapter acceptance results](installed-adapter-acceptance-results.md). The package reuses the bundled domain hooks: it demonstrates registration and checked execution, not an independent application or verifier.

The earlier project validation on 2026-10-23 had **214 unit tests passed**. The initial 17-case Docker/PostgreSQL invocation had **16 passes and one correctly rejected test setup**: the new project recovery test accidentally made the default 300000-ms total budget explicit above the project's 90000-ms ceiling. After correcting the test to preserve omitted defaults, both cleanup-deadline cases passed on the unchanged runtime image. All 17 distinct integration cases have passing observations across these phases; this is not a single all-green 17-case invocation. Live containers confirmed the declared image IDs, CPU/memory/PID limits, and project labels. The CLI discovered a project and selected its sole variant; separate recovery removed recorded project-owned leftovers without consulting YAML or rewriting the report. No Faultline-owned resources remained. See [project acceptance results](project-acceptance-results.md) for measurements and retained evidence.

The earlier adapter phase had 160 unit tests and all 13 Docker/PostgreSQL cases passing in 198.91 seconds. It established malformed-result ERROR handling against a real correct purchase and reran earlier lifecycle regressions. Its measurements remain in [adapter acceptance results](adapter-acceptance-results.md).

The earlier deadline phase had 107 unit tests, an eleven-case full Docker pass, and a final three-case targeted pass after a drain-boundary correction. Its measurements and image identities remain in [deadline acceptance results](deadline-acceptance-results.md).

The earlier recovery phase confirmed that cleanup refuses a live child run, removes its recorded leftovers after a hard kill, preserves original evidence, and leaves a separate environment usable. Its measurements remain in [recovery acceptance results](recovery-acceptance-results.md).

Across successive implementation phases, ordinary purchases passed 20/20 repetitions for each variant. After moving HTTP helper startup outside attempt deadlines, lost-response trials produced the expected duplicate-debit FAIL in 20/20 unsafe runs and PASS in 20/20 correct-A runs, all with required fault coverage. These are separate phase results, not one flawless eighty-trial run on the final revision. A stream close-order warning found during repetition was subsequently fixed and regression-tested.

The original rejected trial, corrections, environment, and evidence locations are documented in [acceptance results](acceptance-results.md). Do not treat example amounts or target counts in the specification as observed results.


## Next boundary

Two milestones remain. M3 has strict replay, comparison, and reproduction groups; dependency-aware reduction, the read-only API, and the React investigation interface remain. M4 covers independent implementations, transfer/defect calibration, CI/release packaging, and measured resource targets. Historical measurements above remain separate from future results.
