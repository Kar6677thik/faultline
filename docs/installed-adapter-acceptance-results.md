# Installed order-adapter validation, 2026-10-23

These measurements cover the installed-package bridge, not completion of Milestone 2 or the public v1 adapter API. The package uses the existing order hooks and verifier. It is not an independent correct application or independent grader.

## Environment and commands

- Debian WSL2, Linux x86-64, Python 3.13.5 coordinator, Docker Engine 28.4.0.
- Root dependencies from the existing `uv.lock`; no new runtime dependency was added.
- Runtime rebuilt after the final Python changes: `faultline-runtime:0.1.0`, resolved ID `sha256:d2a9d592e3390e774048c109abc6ad3389a4fb9bbe87ccfe45153186dc4ac4a1`.
- PostgreSQL: `postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb`.
- Separate `faultline-example-orders` version `0.1.0` wheel built from [the example package](../examples/installed-orders-adapter/pyproject.toml), installed without dependencies in a temporary target, selected by fresh CLI subprocesses.

Commands executed inside Linux/WSL, using the locally available uv executable:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q --junitxml=.faultline/installed-adapter-unit-results.xml
docker build -t faultline-runtime:0.1.0 .
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py \
  --basetemp=.faultline/installed-adapter-validation \
  --junitxml=.faultline/installed-adapter-validation-results.xml
```

Use a **new** basetemp directory for another invocation: pytest replaces an existing directory. Local `.faultline/` evidence is intentionally untracked.

## Observed results

| Check | Measured result |
| --- | --- |
| Ruff lint | Passed |
| Ruff format | 31 Python files already formatted |
| Ordinary suite | 237 passed, 20 Docker tests explicitly skipped, 13.66 seconds |
| Full Docker/PostgreSQL suite | 20 passed in one invocation, 347.07 seconds |
| Installed example adapter, unsafe lost-response purchase | FAIL, two debits, wallet 600000 paise |
| Installed example adapter, corrected lost-response purchase | PASS, one debit, wallet 800000 paise |
| Installed incomplete adapter, corrected lost-response purchase | ERROR at `verify`, one real debit, no accepted partial findings |
| Offline report and scoped cleanup without the package or project file | Passed for all three installed-adapter runs; original report bytes preserved |

All three installed-adapter trials applied the required response-suppression fault, saved selected distribution/factory/content identities, and completed teardown. The full suite also passed the existing bundled scenario matrix, independent read-only observation/isolation checks, project image/resource checks, hard-kill recovery, malformed grading, and stage/cleanup deadline regressions. No Faultline-owned containers or networks remained after validation.

The unit suite adds actual importlib metadata/import tests for missing names, wrong versions, duplicate registrations and duplicate distribution copies, unselected modules, unsafe/missing/FIFO/oversized files, import-source mismatch, reserved-name protection, missing hooks, scrubbed factory failures, preflight expiry, cancellation, and Docker-free validation. It does not replace the wheel/PostgreSQL experiments.

## Selected evidence

| Trial | Run UUID |
| --- | --- |
| Installed example / unsafe | `534b301c-6a86-404c-bafc-0d6196490a94` |
| Installed example / correct | `abc14c7c-baca-4291-ac63-b648467ec0ec` |
| Installed incomplete grader / correct | `38341342-2a0c-4c4c-a322-1cbec475ba97` |

Their bundles are under `.faultline/installed-adapter-validation/test_installed_wheel_runs_and_0/`, `_1/`, and `_2/`, respectively, in `output/runs/<uuid>/`. The built wheel and temporary installation remain under that validation directory's `packaged-adapter0/`. The JUnit files above retain the suite counts and timings.

For each bundle, inspect `adapter.json` for package/engine provenance, `project.json` for selection, `history.ndjson` for response suppression, `snapshots/final.json` for ledger rows and balance, and `report.json` for the distinction between business findings and a broken judge. Cleanup receipts live outside the original bundle.

## Defect found while developing the loader

An initial focused unit invocation had 125 passes and three failures in 14.18 seconds: nonexistent unsafe RECORD entries were not rejected. Python 3.13's `Distribution.files` filters missing files, so the initial implementation could fingerprint only the surviving inventory. The loader was corrected to parse RECORD directly and refuse missing measured files. The final unit and full integration results above follow that correction and the additional duplicate-distribution/import-shadowing checks.

## Limits of these measurements

These are one-run observations per installed scenario, not a reproduction-frequency study. The existing four-pair bundled matrix also used one fresh trial per pair in this invocation. Repetition remains fresh execution, not deterministic replay.

The adapter example inherits Faultline's domain implementation. This demonstrates wheel installation, discovery, checked dispatch, independent database evidence, and lifecycle compatibility without modifying the engine for that package. It does not demonstrate a second domain or satisfy the independent-verifier/second-application release gates. Package hashes are evidence of measured files, not authentication or a sandbox. The precise supported limits and tradeoffs are in the [learning guide](learning/installed-adapters.md).
