# Runnable order projects: measured results

Validation date: 2026-10-23. This phase implements the restricted four-service order-project contract. It does not demonstrate the complete public project/adapter API or finish M2.

## Environment and reproducible commands

Execution used Python 3.12 in Debian WSL2, locked dependencies, and Docker Desktop Linux x86-64. The runtime was rebuilt after all runtime-source changes and before integration testing.

Runtime image ID: `sha256:f8c3c75cbc0194868eed1c67425926f3b5709195d84426a33eff671139adb0f0`.

Equivalent commands from the Linux workspace:

```bash
uv run pytest -q --junitxml=.faultline/project-unit-results.xml
uv run ruff check src tests
uv run ruff format --check src tests
docker build -t faultline-runtime:0.1.0 .
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py \
  --basetemp=.faultline/project-validation \
  --junitxml=.faultline/project-validation-results.xml
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py -k cleanup_deadline \
  --basetemp=.faultline/project-recovery-validation \
  --junitxml=.faultline/project-recovery-validation-results.xml
```

Choose new `--basetemp` paths when repeating a validation phase. Pytest replaces that directory; reusing these names would erase retained evidence.

## Results and the corrected test setup

- **214 unit tests passed**, with 17 Docker cases explicitly skipped, in **9.00 seconds**. The new project-specific suite contributed 54 tests covering invalid settings, paths, budgets, local image resolution, resource enforcement, and ownership.
- Ruff lint passed, and its formatting check confirmed all **29 Python files** were formatted. Project `doctor` also passed for both declared variants without creating experiment resources.
- The first complete integration invocation produced **16 passes and one test-setup rejection in 251.47 seconds**. The rejected project recovery test serialized every scenario default, inadvertently turning the default `total_ms: 300000` into an explicit request above the example project's `90000` ceiling. The engine correctly refused it before creating resources.
- The test was corrected to preserve omitted defaults with `exclude_unset=True` while explicitly setting only its cleanup deadline. **Both cleanup-deadline cases then passed**, with and without a project, in **39.94 seconds**; 15 unrelated cases were deselected. Runtime source and image were unchanged between these phases.
- All 17 distinct integration cases therefore have passing observations across the phases. This was not one uninterrupted all-green 17-case invocation, and the initial rejection remains in its original XML evidence.
- Read-only Docker inventory after completion showed **zero Faultline-managed containers and networks**.

## What the real project checks established

Two project runs used an immutable local application image ID and lower application/worker/observer limits: **192 MiB, 500 CPU millis, and 48 PIDs** each. PostgreSQL retained the default allocation. Tests inspected the live containers and confirmed actual Docker memory, CPU, PID, image, project-label, read-only filesystem, and no-port/no-bind settings.

The unsafe response-suppression experiment produced the expected **FAIL with two debits**; the corrected experiment produced **PASS with one debit**. Both had required fault coverage, complete cleanup, matching configuration hashes, retained `project.json`, and an effective 90000-ms total budget in the plan. Test case durations were **21.685 seconds** and **19.449 seconds** respectively; these include test setup/assertions and are not standalone CLI performance measurements.

A separate CLI subprocess discovered `faultline.project.yaml` in its working directory, selected the sole declared `correct` variant without `--variant`, and passed the actual suppressed-response experiment. Its test case duration was **17.458 seconds**.

The corrected project recovery test deliberately stalled cleanup with real resources present. It retained an ERROR report and unresolved IDs. A separate `clean --run` command removed those resources using recorded ownership without any project YAML in its selected execution context. Original report bytes were unchanged. Cleanup did not reclassify the experiment as a business PASS.

The original four implicit-mode regression runs also passed their expected assertions:

| Scenario / variant | Verdict | Debit rows | Run wall time |
| --- | --- | --- | --- |
| control / unsafe | PASS | 1 | 12.92 s |
| control / correct | PASS | 1 | 11.52 s |
| lost-response / unsafe | FAIL | 2 | 12.40 s |
| lost-response / correct | PASS | 1 | 11.93 s |

These are one fresh trial per pair, not a new twenty-repetition campaign. Isolation, read-only observation, payload conflict, malformed-adapter output, final timeout, missed fault coverage, SIGTERM, SIGKILL recovery, and observation/drain/cleanup deadline regressions were also exercised.

## Retained local evidence

- `.faultline/project-unit-results.xml`: unit suite result.
- `.faultline/project-contract-unit-results.xml`: focused 54-test project contract run.
- `.faultline/project-validation-results.xml` and `.faultline/project-validation/`: initial integration results and artifacts, including the test-setup rejection.
- `.faultline/project-recovery-validation-results.xml` and `.faultline/project-recovery-validation/`: corrected recovery checks, original ERROR reports, ownership manifests, and cleanup receipts.

These paths are ignored by Git and remain local evidence. Previous adapter, recovery, deadline, and initial acceptance measurements remain in their own documents.

## Limits of the claim

The selected immutable application image was still the reference runtime. This proves local selection/pinning and real settings enforcement, not independent external-application compatibility. No external adapter was loaded. Full five-service topology, Toxiproxy, concurrent scenarios, general routes/configuration, strict replay, reduction, React, and release CI remain outstanding.

Host-capacity checks use Docker's reported total CPU/memory; they do not measure current free capacity. Supported lower limits may be too small for some trusted application builds. Path checks assume an ordinary trusted local workspace rather than a hostile concurrent directory-replacement attacker.
