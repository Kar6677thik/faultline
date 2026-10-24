# Milestone 2 acceptance, 2026-10-24

Milestone 2 is complete for the supported HTTP/PostgreSQL experiment engine.
It now runs bounded concurrent workloads, registered generators, acknowledged
network/process faults, authoritative verification, and separately packaged
applications. This is not completion of the v1 release. Strict historical replay,
reduction, React investigation, release calibration, and CI packaging remain M3/M4.

## What was actually run

All Docker experiments used Debian WSL2/Linux x86-64, Python 3.13.5 on the
coordinator, Docker Engine 28.4.0, and Python 3.12 in the runtime image. Dependencies
come from `uv.lock`. Docker reported 14 CPUs and 7,751,798,784 bytes of memory;
these measurements are not the M4 four-core resource qualification.

| Phase | Observed result | Retained local evidence |
| --- | --- | --- |
| Scheduler increment | 6 passed, 60.69 s | `.faultline/m2-scheduler-check-results.xml` |
| Initial proxy attempt | 1 failure: Docker rejected static endpoint IP on an automatically allocated subnet | `.faultline/m2-proxy-check-results.xml` |
| Second proxy attempt | 1 failure: read-back rejected Toxiproxy's normalized listener address | `.faultline/m2-proxy-check-2-results.xml` |
| Diagnostic proxy repeat | Reproduced the listener normalization failure | `.faultline/m2-proxy-diagnostic-results.xml` |
| Corrected proxy checks | 5 passed, 105.25 s | `.faultline/m2-proxy-check-3-results.xml` |
| Quiescence checks | 4 passed, 74.23 s | `.faultline/m2-quiescence-check-results.xml` |
| External domain checks | 2 passed, 45.52 s | `.faultline/m2-external-check-results.xml` |
| Full regression, before final generator refinement | 38 passed in one invocation, 827.82 s | `.faultline/m2-full-validation-results.xml` |
| Final ordinary suite | 299 passed; 40 Docker tests explicitly skipped; 22.24 s | `.faultline/m2-final-unit-results.xml` |
| Final focused PostgreSQL follow-up | 4 passed, 36 deselected, 82.09 s | `.faultline/m2-final-followup-results.xml` |
| Final lint and formatting | Passed for `src`, `tests`, and the counter example; 42 Python files formatted | Terminal checks |

Times above are pytest console elapsed times; JUnit suite times can be slightly
smaller because they omit part of test-run startup/finalization. The full regression
and final follow-up are **different invocations on different runtime builds**.
The final four tests include two existing external retry cases and two new generated
workload cases. This is not a single all-green 40-case invocation on the final build.

The full regression used runtime
`sha256:3b091b50b5a66d3a030f18ac2b9b092bbfd5f605aa94a42b9f06a7d74bba145d`.
After the final source changes, the rebuilt runtime used by the focused checks was
`sha256:9551d87e26c6b75e83611af2d65768191837c0c57c9a572f3446c39317c658b8`.
Individual manifests retain selected image IDs and adapter source inventories.

PostgreSQL was pinned to
`postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb`.
Toxiproxy 2.12.0 was pinned to
`ghcr.io/shopify/toxiproxy@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e`.
The external counter wheel/image was rebuilt by the integration fixture against
the selected runtime. No Faultline-owned containers or networks remained after
the final follow-up, checked by the `io.faultline.run` label.

## Acceptance map

The requirements are in [delivery and acceptance](../specs/10-delivery.md).
Unit tests establish scheduler/input/oracle contracts; real PostgreSQL tests check
actual effects and lifecycle behavior. Neither substitutes for the other.

| Gate | Evidence and scope |
| --- | --- |
| AT-007: plan stability | [Scheduler tests](../tests/test_scheduler.py) and [registered generator tests](../tests/test_generators.py) compare canonical plans across three separate processes. Saved concrete operations reconstruct without a generator. Final real generated-order/counter runs verify resulting rows. The historical replay command remains AT-014/M3. |
| AT-008: scheduler semantics | [Scheduler tests](../tests/test_scheduler.py) observe genuine bounded overlap, barrier/dependency admission, fault gates, retry backoff, unchanged request bodies/keys, conditional retries, unused attempts, and cycle rejection. [Worker tests](../tests/test_worker.py) exchange interleaved commands and reversed results over a real socket pair. Real concurrent duplicate and independent order cases both pass. |
| AT-009: fault contracts | `test_acknowledged_fault_matrix` runs latency, disconnect, and restart individually. `test_proxy_disable_breaks_existing_and_new_database_connections` probes established and new connections. [Fault tests](../tests/test_faults.py) require positive acknowledgments, duration from acknowledgment, exact healing, and ownership. A separate latency-plus-suppression run demonstrates supported composition. |
| AT-010: quiescence | Real delayed handlers outlive client timeouts; the completed purchase passes only after process/session evidence. A killed delayed handler leaves no purchase and correctly fails required progress. Registry reconciliation and zero application sessions are asserted. The real drain deadline case is INCONCLUSIVE, never premature PASS. |
| AT-011: unsafe input | [Scenario](../tests/test_scenarios.py), [project](../tests/test_projects.py), [generator](../tests/test_generators.py), [environment](../tests/test_environment.py), [fault](../tests/test_faults.py), and [creation-recovery](../tests/test_creation_recovery.py) tests reject unsafe URLs/configuration, invalid dependencies/capabilities, foreign ownership, and unsafe runtime access. Real isolation checks probe the network boundary and read-only observer. |
| AT-012: harness failures | [Adapter tests](../tests/test_adapters.py) inject preparation/readiness/observation/verification failures and malformed evidence. Integration closes the actual worker stream, injects ENOSPC at the artifact boundary, interrupts real helpers, sends SIGTERM/SIGKILL, and stalls cleanup before scoped recovery. Cleanup-denial and Ctrl+C behavior also have unit coverage. ERROR retains available findings and never counts as business resilience. |
| AT-013: external application | The separately built [counter package](../examples/counter-adapter/) supplies its own application, tables, snapshot helper, generator, and pure verifier. Real unsafe/correct retry and generated cases run without core domain changes. Offline reporting works without importing that package. |

All named integration functions are in [test_integration.py](../tests/test_integration.py).
The full regression also retained M1 controls, final-timeout semantics, missed-fault
INCONCLUSIVE, malformed-grader ERROR, resource settings, hard-kill ownership
recovery, and independent read-only observation checks.

## Final follow-up observations

| Scenario | Actual verdict and authoritative state | Run UUID |
| --- | --- | --- |
| External counter, unsafe lost response | FAIL; two increments totaling 14 | `a7e62a08-8209-4c0e-b825-292d185438c1` |
| External counter, correct lost response | PASS; one increment totaling 7 | `25f49097-33af-48ac-96b2-97b848634530` |
| External generated counter, correct | PASS; three increments totaling 186 | `3c5dabb5-aa50-4e0c-bcc5-1c0b207e6ccc` |
| Bundled generated orders, correct | PASS; three purchases with 600000 paise total debits | `4c6cd60d-0990-4876-a486-22cb7aeb39e1` |

Bundles are beneath `.faultline/m2-final-followup/`, in each test's `runs/<uuid>/`.
Full regression bundles are beneath `.faultline/m2-full-validation/`. These local
artifacts are intentionally untracked; the measured summary and reproduction
commands are part of the repository. Reports link findings to actual snapshot rows.

## Repeat the checks

From the repository in Linux/WSL, with the pinned dependency images already pulled:

```bash
uv run ruff check src tests examples/counter-adapter/src
uv run ruff format --check src tests examples/counter-adapter/src
uv run pytest -q
docker build -t faultline-runtime:0.1.0 .
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py
```

For the final focused selection:

```bash
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py \
  -k 'independent_counter or builtin_generated'
```

Use a **new** `--basetemp` directory when retaining artifacts; pytest replaces an
existing basetemp. Add `--junitxml=<new-path>` to preserve machine-readable results.
The current suite contains 40 Docker cases. The 38-case full regression took about
14 minutes; this diagnostic suite is not the selected future PR matrix, and it
does not demonstrate the M4 ten-minute CI target.

## Corrections and limits

The proxy provisioning failures were harness defects, not application findings.
A control-only DNS alias replaced the rejected static IP; read-back now recognizes
Toxiproxy's equivalent normalized data listener while still checking the port and
upstream. The corrected individual fault tests passed before composition testing.

The disk test raises `OSError(28, ...)` at an artifact write boundary. It exercises
cleanup/error handling without filling the host disk. It does not prove every
filesystem failure or power-loss boundary. Similarly, the external counter
demonstrates onboarding, not M4's independent correct-B or transfer application.

Supported limits remain explicit: five service roles, PostgreSQL authority, bounded
registered HTTP/JSON work, one proxy fault per binding and one lifecycle fault per
service in a scenario. Canonical fault bindings are `database` and `application`.
Uninstrumented restart cannot promise a specific SQL crash boundary. The transfer
checkpoint is part of the future M4 application. Seeded inputs are repeatable;
thread/OS/database scheduling is not deterministic. These selected runs do not
establish a population failure rate or replace the later twenty-trial release corpus.

The [experiment walkthrough](learning/experiment-engine.md),
[external adapter guide](learning/external-adapters.md), and
[decisions L-037 through L-045](learning/decisions.md#l-037-materialize-values-before-concurrent-admission)
explain why each component exists, alternatives, source/test mappings, and limits.
