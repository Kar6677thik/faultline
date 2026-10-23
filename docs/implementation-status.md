# Implementation status: first critical slice

This file describes executable behavior. The numbered files in `specs/` remain the target v1 contracts; they must not be mistaken for features already shipped.

## Available

- Python package and CLI commands: `doctor`, `validate`, `run`, `report`, and scoped `clean --run`.
- Strict YAML validation for one positive `create_order` operation in `orders_v1`/`standard_wallet`, concurrency one, at most five attempts, explicit bounded backoffs, and at most one response-suppression fault. All seven order invariants are required. Unknown fields and unsupported features fail validation.
- Real FastAPI/PostgreSQL order processing with deliberately unsafe and correct-A variants. Correct-A uses account-row locking, unique purchase/token constraints, and one transaction across all business effects. Conflicting payloads are rejected without new effects.
- A separate worker performs actual HTTP calls within private Docker networks. The coordinator suppresses the first matching successful response from the client policy and durably records that decision before retrying.
- A pure verifier reads complete snapshots from separate read-only PostgreSQL observation. It preserves duplicate rows and checks identity, atomicity, conservation, inventory, outbox intent, response consistency, and completion.
- Fresh isolated resources for every run/repetition, per-run labels and ownership nonce, resource limits, no published ports/host binds/socket mounts, read-only container filesystems with explicit ephemeral writable areas, bounded service logs, and cleanup in failure paths.
- JSON/Markdown reports, NDJSON event history, canonical snapshots, image identities, evidence hashes, offline integrity checking, and repeat outcome counts.

## Explicit milestone decisions

1. **Two internal networks, no proxy yet.** The client network connects worker/application; storage connects application/database/observer. Database connections go directly to the run-owned database. The four-network Toxiproxy topology in the v1 specification becomes necessary in Milestone 2. No database-network fault capability is advertised now.
2. **Built-in trusted environment.** There is no external project manifest or adapter loader yet. `--project`, arbitrary image/URL input, other domains, and additional service graphs are unsupported. The built-in runtime tag is resolved to its immutable local image ID before execution. The PostgreSQL image and Python base image are pinned by digest.
3. **Observation after graceful shutdown.** Work admission stops when the one logical operation finishes. The coordinator requests graceful application exit, then confirms zero application PostgreSQL sessions before treating the final state as quiescent. A forced kill or uncertain state produces UNKNOWN/INCONCLUSIVE. No restarted application or background worker can write afterward in this profile. The continuously running in-flight registry remains Milestone 2 work.
4. **Simple bounded helper protocol.** The HTTP worker starts once, acknowledges readiness, and receives correlated JSON requests over a persistent Docker exec stream. Python startup is outside attempt deadlines. Setup and snapshot helpers use bounded one-shot exec calls. The coordinator owns retry policy, fault application, event durability, and display order. This is not the concurrent event/acknowledgment protocol specified for Milestone 2. Worker timestamps are supplemental; different sources' monotonic clocks are not compared.
5. **Fixed profiles and explicit limits.** Timeout 50–10000 ms, backoff 0–1000 ms, execution budget at most 60 seconds, and at most five attempts. Each helper, database statement, readiness wait, and Docker API request has a bound. The full configurable total-stage/cleanup deadline framework and process-crash recovery are not implemented. Container memory is capped at 256 MiB each, one CPU each; database files are disposable tmpfs, so this does not test power-loss durability.
6. **Repetition is not replay.** `--repeat` reuses the validated plan and pinned current images with a fresh database each time. Strict historical replay, environment-difference comparisons, statistical reproduction groups, and reduction are not yet commands. No claim of deterministic OS scheduling is made.
7. **Fail closed and preserve evidence.** Corrupt or missing evidence is never presented as PASS. A full filesystem can prevent final report writing; the incomplete bundle remains diagnosable and ownership records support scoped cleanup. Artifact capture has a 50 MiB ceiling plus a small final diagnostic allowance. A configurable whole-group disk budget and broad secret sanitizers are later work; only synthetic bundled payloads are accepted now.
8. **Execution lock scope.** CLI runs/cleanup serialize within the selected output directory. Different output roots can run independently, protected by unique resource identities. The eventual workspace-wide coordinator lock is not claimed.

## Verification status

Measured on 2026-10-23: 54 unit tests passed; the eight opt-in Docker integration tests passed on the final runtime image; Ruff lint and formatting checks passed. Final checks included both scenario/variant pairs, isolation, read-only observation, payload conflicts, committed purchases with final client timeouts, missed fault coverage, and real SIGTERM cleanup. No Faultline-owned containers or networks remained afterward.

Across successive implementation phases, ordinary purchases passed 20/20 repetitions for each variant. After moving HTTP helper startup outside attempt deadlines, lost-response trials produced the expected duplicate-debit FAIL in 20/20 unsafe runs and PASS in 20/20 correct-A runs, all with required fault coverage. These are separate phase results, not one flawless eighty-trial run on the final revision. A stream close-order warning found during repetition was subsequently fixed and regression-tested.

The original rejected trial, corrections, environment, and evidence locations are documented in [acceptance results](acceptance-results.md). Do not treat example amounts or target counts in the specification as observed results.

## Next implementation boundary

Before expanding into Milestone 2, retain this defect and corrected scenario as regression cases. Next work should introduce the versioned public adapter/project contract, run-wide deadlines and stronger recovery, and the proxy topology. Add concurrent workloads and network faults only after their evidence and quiescence contracts are tested. React belongs after useful report bundles exist; there is no placeholder frontend in this slice.

The user-supplied engineering guide is reflected in [engineering principles](engineering-principles.md). Deliberate scope limits are documented decisions, not implicit changes to the v1 requirements.
