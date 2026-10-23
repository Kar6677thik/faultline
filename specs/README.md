# Specification baseline

Version: 0.1. Date: 2026-10-23. Status: v1 specification baseline. Implementation was subsequently authorized by the user and has begun; see [current implementation status](../docs/implementation-status.md) for the supported milestone subset. These specifications describe the complete target release, not only existing commands.

The user selected a local CLI, React report viewer, and CI for the first usable release. All other choices below are explicit engineering defaults for this baseline, not additional user commitments. They may change through a documented decision revision before affected implementation begins.

## Reading order and ownership

1. [Product and scope](01-product.md): users, workflows, release boundary, requirements, and non-goals.
2. [Architecture and stack](02-architecture.md): components, deployment, isolation topology, lifecycle, and concurrency.
3. [Project and scenario contracts](03-scenarios.md): configuration, identities, scheduling, retries, and limits.
4. [Application adapter and reference domains](04-adapters.md): onboarding, state observation, order and transfer contracts.
5. [Fault semantics](05-faults.md): precisely what each fault does and what evidence proves it happened.
6. [Correctness and verdicts](06-verification.md): independent checks, evidence sufficiency, outcomes, and diagnosis.
7. [History, replay, and reduction](07-evidence-replay.md): artifacts, compatibility, reproducibility, and minimization.
8. [CLI, local API, and React viewer](08-interfaces.md): commands, exit codes, report API, and investigation UX.
9. [Safety and operations](09-safety.md): allowed targets, teardown, trust, data handling, and resource limits.
10. [Delivery and acceptance](10-delivery.md): milestone gates, verifier calibration, CI, and traceability.
11. [Decisions and limitations](11-decisions.md): chosen defaults, alternatives, unresolved research, and deferred scope.

## Specification conventions

- MUST / MUST NOT are requirements. SHOULD is a preferred behavior whose exception requires an explanation in the implementation change. MAY is optional.
- A release requirement is identified as `FL-xxx`. Acceptance cases are `AT-xxx` in the delivery document.
- `v1` means the first usable release after all four milestone gates, not the first demonstration.
- Milliseconds are integer durations. Wall-clock timestamps use UTC RFC 3339. Money uses integer minor units; ₹2,000 is `200000` paise.
- Examples are illustrative contract documents, not generated schemas or implementation files. Unknown configuration fields MUST be rejected rather than ignored.
- Each topic has one normative owner in the reading list. Examples elsewhere cannot override its contract. A discovered contradiction MUST be resolved in the documents before the affected feature is considered ready.
- Changes to persisted semantics, verdict rules, or fault behavior MUST revise the relevant version or record a compatible clarification. Do not silently reinterpret old evidence.

## Shared vocabulary

- **Project:** trusted local configuration describing an application, adapter, images, and scenarios.
- **Application under test (AUT):** backend and dependencies created specifically for an experiment.
- **Experiment:** a request to execute one expanded scenario against one application variant.
- **Run:** one execution from a fresh fixture; the unit of isolation, evidence, and verdict.
- **Logical operation:** a business intent, such as one purchase; distinct from its HTTP attempts.
- **Attempt:** one transport submission. A retry retains the logical operation identity and request content.
- **Scenario:** bounded operations, dependencies, barriers, fault rules, and invariant selection.
- **Plan:** validated, fully expanded scenario with concrete IDs, inputs, and fault bindings.
- **History:** durable observations of what actually happened; a plan alone is not a history.
- **Invariant:** a declared correctness property checked against specified authoritative evidence.
- **Authoritative snapshot:** a complete, coherent read of business records from the adapter's declared source.
- **Replay:** a fresh execution of a saved plan and environment identity, not restoration of OS scheduling.
- **Reduction:** dependency-aware deletion of scenario elements followed by measured reruns.
- **Quiescence:** the adapter's declared condition that no admitted business work remains in progress.

## Baseline completeness

This pass settles release scope, supported platforms, safety boundaries, operation identity, retry ambiguity, fault triggering, evidence completeness, pass/fail precedence, lifecycle, artifact compatibility, replay claims, reduction limits, CLI/API/UI responsibilities, and release acceptance.

Exact dependency patch versions and measured performance remain implementation-time decisions. They MUST be recorded when implemented. No external company article or numerical claim from the original proposal is treated as verified evidence or as a product requirement; the requirements stand independently of those motivations.
