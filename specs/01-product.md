# Product and scope

## Outcome

Faultline helps a backend engineer answer: “Under this declared failure scenario, did my application preserve its business rules, and what evidence lets me investigate the result?”

The primary user is a developer running a trusted application locally or in a disposable Linux CI job. A second user is a reviewer investigating a saved failure bundle without rerunning the application. The first release assumes one operator per local installation and no remote clients.

## Required workflows

1. Run a bundled known-bad order backend and see an ordinary purchase succeed without injected faults.
2. Run the same backend with a controlled lost response and retry; inspect duplicate debit evidence.
3. Run a corrected variant under the identical plan and see all required invariants pass.
4. Connect another trusted backend through an adapter and a constrained environment manifest.
5. Replay a saved experiment several times and inspect the counts of matching failures, other failures, passes, and inconclusive/error outcomes.
6. Reduce a failing plan within a fixed budget and inspect both original and reduced evidence.
7. Run checked-in scenarios in CI, fail the job appropriately, and retain readable and machine-readable reports.
8. Open local artifacts in React and follow a violation back to attempts, applied faults, and authoritative records.

## Requirements

- **FL-001 — Disposable execution:** Every run MUST use a new environment and verified baseline state. A repeat or reduction candidate MUST NOT reuse mutated application storage.
- **FL-002 — Usable integration:** The same Python core MUST serve CLI and CI. A third-party backend MUST be integrable without editing core engine source, using a trusted Python adapter and the supported manifest.
- **FL-003 — Logical identity:** Requests, attempts, retries, and business identities MUST be distinguishable and correlated without inferring completion from HTTP timeouts.
- **FL-004 — Explicit fault evidence:** Faults MUST record eligibility, application, healing, and application failures. A requested fault is not proof of an applied fault.
- **FL-005 — Independent verification:** A verdict MUST use declared authoritative state and account for missing or inconsistent evidence. Logs and HTTP status alone are insufficient for monetary correctness.
- **FL-006 — Honest outcomes:** PASS, FAIL, INCONCLUSIVE, and ERROR MUST have the distinct meanings in the verification specification.
- **FL-007 — Durable evidence:** Every started run MUST leave a report or recoverable incomplete bundle, including the effective plan and environment identity.
- **FL-008 — Measured replay:** Reproduction MUST be reported as measured outcomes across fresh runs. Seeds MUST NOT imply deterministic concurrency.
- **FL-009 — Reduction:** The tool MUST attempt bounded, dependency-aware deletion and preserve the target violation identity. It MUST report the reduction budget and observed reliability.
- **FL-010 — Safe targeting:** Fault targets MUST be resources created and owned by the current run. Arbitrary endpoint, container, volume, or daemon targeting MUST NOT be supported.
- **FL-011 — Investigable UI:** The React viewer MUST show verdicts, missing evidence, chronology, invariant explanations, replay outcomes, and reduction results, with links to raw records.
- **FL-012 — CI contract:** Exit codes and JSON reports MUST allow CI to distinguish correctness failure from invalid configuration, inconclusive evidence, and harness problems.
- **FL-013 — Calibrated verifier:** Release validation MUST include at least five known defect types, two independently implemented correct variants, and a second business domain.
- **FL-014 — Bounded resources:** A run, artifact bundle, repeat group, and reduction job MUST obey declared limits and perform scoped cleanup on interruption.
- **FL-015 — Versioned contracts:** Plans, histories, reports, adapters, and fault semantics MUST identify their versions; incompatible artifacts MUST be rejected explicitly.

## First usable release

v1 includes HTTP/JSON workloads; sequential and barrier-released concurrent attempts; deterministic input expansion; explicit retry policies; client-visible response suppression; dependency latency and connection interruption through Toxiproxy; restart of an AUT application process represented by a dedicated container; PostgreSQL snapshots; report bundles; measured replay; bounded reduction; local React viewing; and Linux CI.

The order reference domain includes wallet debits, inventory, orders, and a transactional confirmation outbox. A second reference domain transfers simulated funds between accounts. An outbox row is a durable publication intent, not proof of message delivery.

The integration boundary is deliberately explicit: an application supplies fixtures, authority mappings, quiescence detection, and invariants through an adapter. Faultline does not discover arbitrary business rules from source code, schemas, or OpenAPI.

## Non-goals for v1

- Production chaos engineering, remote endpoints, Kubernetes, or faulting the user's existing services.
- Hosted execution, multi-user accounts, team permissions, remote report sharing, or a web interface that starts runs.
- Full linearizability/serializability checking, a general distributed model checker, or a proof of correctness.
- Deterministic replay of CPU scheduling, database interleavings, packets, or external clocks.
- Kafka/Redis integration, message brokers, message duplication/reordering, or end-to-end event consumption checks. These require a later adapter and fault contract.
- Automatic code repair, LLM-generated invariants, agent benchmarking, payment-provider connections, or real money.
- Load testing at production scale, performance-regression gating, or an observability platform.
- Automatic general causal explanations, global-minimum counterexamples, or a universal adapter for arbitrary applications.

## Success and claims

Release success is measured by the acceptance gates, not by an arbitrary quantity of injected faults. Every published demonstration MUST distinguish intended acceptance targets from measured results and record machine/environment details.

A report may state “this run violated at-most-one debit for purchase P, after fault F was observed.” It MUST NOT claim that F caused the violation solely because F preceded it. A clean control and matched reproduction can strengthen the explanation; absence of other races is still not proven.
