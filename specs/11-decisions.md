# Decisions and limitations

These records make defaults explicit so implementation does not quietly choose incompatible meanings. They are accepted for this specification baseline and can be revised with rationale and affected-contract updates.

## D-001 — Python and React

Choose Python for asynchronous orchestration, adapter authoring, verification, and sample services; choose React/TypeScript for investigation. This follows the user's stack preference and is suitable for bounded I/O workloads. Do not introduce .NET or Angular dependencies. High packet throughput and microsecond scheduling are outside v1 requirements, so Python is not being asked to provide kernel-level timing.

## D-002 — Local CLI, viewer, and CI

The user explicitly selected this deployment model. The CLI owns execution; the web server is local and read-only. Defer hosted workers, team accounts, authentication beyond local access protection, persistent job queues, and remote execution. This removes multi-tenant execution from the first release's trust model.

## D-003 — Linux containers, WSL2 on Windows

Support Linux x86-64 execution first so container lifecycle and process signals have one meaning. Windows developers use WSL2 with Docker Linux containers. A native Windows Python viewer might be feasible later, but native Windows fault execution is not part of the baseline. Verify installation practicality at AT-025.

## D-004 — Engine-owned Docker resources

Choose constrained service manifests and direct Docker lifecycle management over arbitrary Docker Compose execution or adopting existing environments. This makes ownership verification and target restrictions enforceable. Compose may later help developers build images, but its broad settings are not silently accepted as experiment configuration. A user's running k3s services are never targets.

## D-005 — Adapter-based correctness

An adapter explicitly defines identities, fixtures, quiescence, authoritative state, and invariants. Automatic source analysis, OpenAPI-derived business rules, and LLM grading are deferred. The cost is real onboarding work; the benefit is a check whose evidence and limitations can be inspected.

## D-006 — PostgreSQL plus file artifacts

PostgreSQL holds AUT business state. Faultline's evidence is versioned files; optional SQLite is only an index. This avoids confusing the tool's storage with the database being faulted, keeps reports portable, and allows offline review without running PostgreSQL or Docker.

## D-007 — Honest controlled response loss

Use a client transport wrapper for the first exact response-suppression scenario. This is intentionally distinguishable from Toxiproxy network faults. Timing a TCP reset “just after commit” without transaction instrumentation is not a reliable first milestone. The wrapper preserves a real backend interaction while controlling what the client retry policy observes.

## D-008 — Evidence before a green result

PASS requires complete required checks and fault coverage. Distinguish FAIL, INCONCLUSIVE, and ERROR, and retain violations even when harness errors dominate the overall verdict. Do not collapse unknown state into successful resilience or timeout into failed business execution.

## D-009 — Repeatable inputs, measured execution

Save expanded plans, dependency versions, fixtures, and immutable images. A seed determines generated inputs, not database scheduling. Strict replay and explicit variant comparison are different operations. Frequency is measured over all trials with denominators visible.

## D-010 — Bounded deletion reduction

Start with dependency-aware deletion of operations, faults, and retry suffixes, a fixed target signature, repeated trials, and a hard budget. Do not implement a global optimizer, arbitrary request-value shrinking, or probabilistic guarantees in v1. A smaller verified reproduction is already useful.

## D-011 — Durable outbox intent, no broker guarantee

The order example checks a durable confirmation intent in PostgreSQL. It does not claim that an event was delivered exactly once. Broker failures, redelivery, message reordering, and consumer deduplication require a subsequent delivery-specific contract and sample application.

## D-012 — A usable release is larger than the first demo

The first milestone is one failure and one correct implementation. The first usable release additionally requires public adapter contracts, robust evidence, network/lifecycle faults, replay/reduction, React, CI, and independent validation. Do not call the milestone demo the complete product. Avoid promising the original 8–12 week estimate before measuring implementation pace.

## Decisions intentionally left to implementation

These are constrained choices, not missing product semantics:

- Exact compatible dependency patches, container digests, build/release packaging tool, and lockfile format. Pin them, document them, and satisfy offline report-viewing and reviewer setup requirements.
- UI component library, visual styling, and browser acceptance framework, subject to accessibility, performance, and no external asset requirements.
- The internal Python module layout and whether a rebuildable SQLite catalog is needed. Neither can change persisted contracts or result semantics.
- Detailed correct-B transaction implementation, provided it is independent of correct-A and conforms to the same adapter/domain properties.
- Numeric measurements of time, memory, detection frequency, and reduction quality. These must come from actual runs and cannot be settled by writing a specification.

When implementation uncovers a contract conflict, revise the owning specification and affected examples/tests before committing to an incompatible behavior. New major capabilities such as remote execution, real production data, arbitrary Compose ingestion, message brokers, or AI-agent evaluation require a separate scope decision.

## Known limits to publish with v1

- Safety depends on a trusted local Docker daemon, images, and adapter code; containers are not a malicious-code sandbox.
- Business verification is only as sound as the adapter's complete, independently validated observations. Applications without reliable quiescence cannot receive unqualified final-state PASS.
- Request recording and fault coordination affect timing. Observed races and frequencies describe this harness and environment.
- The bounded corpus and two correct variants calibrate the tool; they do not establish a universal false-positive/false-negative rate.
- The reducer can miss simpler or less stable failures and may stop early due to time/disk budget. It does not prove minimality.
- An interrupted run may leave owned Docker resources that need explicit cleanup. Ownership checks take precedence over aggressive cleanup.

The company examples in the supplied proposal are motivation only. This specification does not verify their publication dates, quote their findings as benchmark evidence, claim affiliation, or depend on their APIs. Source research and an optional AI-agent comparison study can be added later without changing Faultline's core purpose.
