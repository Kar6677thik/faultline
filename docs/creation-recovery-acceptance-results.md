# Creation-intent recovery validation, 2026-10-24

This increment addresses coordinator death after Docker creates a resource but before its returned ID is saved. It also fixes an event-sequence interruption defect found by the real SIGTERM regression. It does not complete Milestone 2, prove machine-crash durability, or resolve indefinitely absent pending creations.

## Environment and commands

- Debian WSL2, Linux x86-64, Python 3.13.5 coordinator, Docker Engine 28.4.0.
- Existing root `uv.lock`; no new runtime dependencies. The missing temporary uv executable was replaced with the same uv 0.13.5 under the WSL user's home directory.
- Corrected runtime image rebuilt after the final source changes: `faultline-runtime:0.1.0`, resolved ID `sha256:37297fe0f3833102327a3277a148bd1ab634ba91b72df028e08f3e17608ceb98`.
- PostgreSQL remained `postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb`.

Final-source commands, executed in Linux/WSL with the available uv executable:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q --junitxml=.faultline/creation-recovery-unit-corrected-results.xml
docker build -t faultline-runtime:0.1.0 .
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py \
  --basetemp=.faultline/creation-recovery-corrected \
  --junitxml=.faultline/creation-recovery-corrected-results.xml
```

Always choose a new `--basetemp` path for a later invocation: pytest replaces its contents. Local evidence under `.faultline/` is intentionally untracked.

## Measured checks

| Check | Outcome |
| --- | --- |
| Ruff lint | Passed |
| Ruff format | 33 Python files already formatted |
| Corrected-source ordinary test suite | 260 passed, 22 Docker tests explicitly skipped, 10.39 seconds |
| Corrected full Docker/PostgreSQL suite | 22 passed in one invocation, 310.91 seconds |
| SIGKILL after network creation, before ID recording | Recovered the exact ID, cleanup twice succeeded, original evidence unchanged |
| SIGKILL after container creation, before ID recording | Recovered the exact ID, cleanup twice succeeded, original evidence unchanged |

The corrected suite also passed the earlier SIGTERM failure point, protected-environment hard-kill recovery, cleanup deadline/recovery, project resource/isolation checks, installed-wheel checks, and unsafe/correct real PostgreSQL scenarios. No Faultline-owned containers or networks remained after the run. Ruff formatting and all 188 checked local documentation/specification links passed.

Creation-window run IDs in `.faultline/creation-recovery-corrected/`:

| Case | Run UUID | Reconciled immutable Docker ID |
| --- | --- | --- |
| Client network | `5c12f573-a51b-4603-8b94-6f366ea7bf0a` | `da084ae21345c7314637a9d6bcb06e7fb96ecba209110fd9fdb7dc54aee95fd7` |
| Database container | `e335e304-7e43-4334-a641-8253ea580f58` | `edfa2109d0503d43bb7eb8b8c2976c565b4ecb5fa746683dead0190160b2e9c6` |

Each case's pytest directory contains `interrupted-create/runs/<uuid>/` for original evidence and `interrupted-create/cleanup/` for receipts and the saved binding. The final JUnit record is `.faultline/creation-recovery-corrected-results.xml`. The original manifests remain without the interrupted resource ID; the binding files record it separately.

Unit coverage includes durable write-before-create/delete ordering; failure to save an intent; missing, wrong-name, wrong-token, and wrong-role objects; binding write failure; duplicate/malformed intents; invalid binding records; symlink refusal; whole-selection binding validation; immutable-ID reuse after deletion; and a late object appearing after an unresolved absence. The existing legacy recorded-ID, locking, project, adapter, verifier, and deadline tests remain in the suite.

Two artifact tests deterministically interrupt after a real event `fsync` and inject a partial write. The complete event is retained and counted once; partial bytes are preserved and never finalized as a completed report.

## Initial integration failure and correction

The first full invocation stopped with **14 passes and one failure in 190.70 seconds**. It used runtime ID `sha256:282bc515350ec68a7a1791f129a11ec8006b6f3e24b6690434895c85f269c9d7` and retained evidence under `.faultline/creation-recovery-validation/`, with JUnit output `.faultline/creation-recovery-validation-results.xml`.

`test_real_sigterm_cleans_only_the_child_run` successfully interrupted and cleaned its child, but `read_bundle` rejected the completed history. Run `26660924-7ade-40fe-b58e-82a7d1f1d04f` contained a `ready` lifecycle event numbered 4 and a subsequent cleanup event also numbered 4. The signal arrived after the event bytes were written but before the sequence counter advanced.

`Bundle.event` now reconciles exactly the attempted append on an exception. Complete matching bytes advance the counter; absent bytes do not; partial or unreadable data blocks further appends and completed-report finalization. It re-raises the original interruption and does not block signals across disk writes. The runtime was rebuilt before the corrected full invocation. The failed bundle is retained unchanged as evidence of the discovered defect.

## What the real creation-window tests check

The test starts a child coordinator and pauses it after Docker returns a resource but before `Environment.record` saves the ID. One case targets the client network; the other targets the database container. A test-only marker provides timing and an independent ID oracle; it is outside the experiment bundle and is not read by recovery.

The assertions require the resource to exist with its creation labels while its ID is absent from the manifest; live cleanup to refuse the root lock; SIGKILL to terminate the child; ordinary cleanup to persist a matching recovery binding and remove the owned leftovers; and a second cleanup to succeed using the same ID. Every original experiment file must remain byte-for-byte unchanged. The broader suite retains the separate protected-environment regression.

These are fresh experiments and process-interruption checks, not replay or statistical reproduction-frequency measurements. An absent pending intent intentionally stays incomplete; there is no claim of eventual cleanup when Docker never materializes the resource. The [learning guide](learning/creation-recovery.md) explains the supported ownership contract, file layout, and remaining limitations.
