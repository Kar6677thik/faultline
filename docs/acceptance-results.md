# First milestone acceptance results

Measured locally on 2026-10-23. These results cover the built-in order fixture and controlled client response suppression; they are not a benchmark of arbitrary applications or the full v1 release.

## Environment

- Debian WSL2, Linux x86-64, kernel `6.6.87.2-microsoft-standard-WSL2`.
- Docker Desktop Linux engine `28.4.0`; host Python `3.13.5`; runtime Python `3.12.15`.
- Locked Python dependencies from `uv.lock`; pinned PostgreSQL image from the Docker configuration.
- Final runtime image ID: `sha256:ab16e5a884d92ff4c2a58ece1da98493a0a324516e31cfd9d159979b2c5966af`.
- Every experiment used fresh containers, internal networks, and a fresh PostgreSQL fixture. Database storage was tmpfs.

## Repeated experiments

The repetitions occurred across successive implementation phases, not one uninterrupted eighty-trial run on the final revision.

| Phase | Scenario / variant | Observed result | Required fault coverage |
| --- | --- | --- | --- |
| Initial helper transport | Ordinary purchase / unsafe | 20/20 PASS | No fault requested |
| Initial helper transport | Ordinary purchase / correct | 20/20 PASS | No fault requested |
| Persistent HTTP worker | Lost response / unsafe | 20/20 expected business FAIL | 20/20 applied |
| Persistent HTTP worker | Lost response / correct | 20/20 PASS | 20/20 applied |

Each successful acceptance trial checked the report, evidence integrity, actual ledger rows and wallet balance, cleanup completion, and absence of resources carrying that run's label. The unsafe lost-response trials recorded two debits totaling `400000` paise for one `200000`-paise purchase. Correct trials recorded one debit of `200000` paise.

An additional initial fault trial failed acceptance: starting a new Python helper for each attempt consumed the one-second request deadline, leaving the required response-suppression fault unapplied. The application still duplicated the charge after an ordinary timeout, but that did not satisfy the intended fault experiment. This evidence is retained. Starting one persistent worker before workload dispatch fixed the timing problem.

The repeated persistent-worker trials exposed a Docker stream close-order warning. Closing the owning HTTP response before its raw stream/socket fixed it; a real socket-pair regression test now covers that behavior. Unraisable exceptions are treated as pytest errors.

## Final checks

The final unit suite passed: **54 passed, 8 Docker tests skipped**. Ruff lint and formatting checks passed for all 20 Python files. The Docker tests are run separately with explicit opt-in.

The final integration confirmation passed **8/8 tests in 93.09 seconds**, with no warnings reported:

- Both variants passed ordinary purchases; response suppression exposed duplicate debits in unsafe and preserved one debit in correct.
- Internal network/container isolation, read-only observer credentials, and rejection of conflicting idempotency payloads were checked against real services.
- A committed purchase with a final client timeout still passed business verification.
- An unmatched required fault produced INCONCLUSIVE even with passing business invariants.
- Sending SIGTERM to a real child CLI produced exit 130, retained interrupted evidence, and cleaned its owned resources.

After the suite, Docker label-filtered inventories found **zero Faultline-owned containers and zero Faultline-owned networks**. No global prune was used.

## Retained evidence

These directories are local, ignored artifacts, not committed fixtures:

- `.faultline/acceptance-20261023/`: forty passing control trials and the rejected initial fault trial.
- `.faultline/acceptance-persistent-faults/`: forty repeated fault trials after introducing the persistent worker.
- `.faultline/final-validation/`: reports from the first final integration invocation; its terminal exit result was unavailable after session continuation.
- `.faultline/final-confirmation/`: fresh final integration confirmation, with pytest results in `.faultline/final-confirmation-results.xml`.

Run bundles contain the effective plan, image IDs, correlated history, authoritative snapshots, findings, and cleanup records. See [README verification commands](../README.md#verification) to repeat the checks on another machine.

## Limits of the evidence

Twenty successful repetitions are an observed frequency under this environment, not a guarantee of deterministic replay or absence of defects. This milestone tests one operation with sequential attempts and one deliberately flawed backend. It does not establish concurrency correctness, TCP packet-loss behavior, power-loss durability, support for external applications, or the release target of five defect types and two independently implemented correct variants. See [implementation status](implementation-status.md) for the exact supported contract and deferred work.
