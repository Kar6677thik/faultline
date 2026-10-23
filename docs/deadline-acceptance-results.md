# Deadline increment: measured results

Measured on 2026-10-23 in Debian WSL2 with the local Linux x86-64 Docker engine. This increment adds coordinator stage/total budgets with reserved cleanup/reporting time. It does not establish hard real-time behavior for every possible kernel/native operation or implement a total deadline for standalone recovery commands.

## Validation phases

- Final unit suite: **107 passed, 12 Docker tests skipped** in 3.41 seconds. Opt-in Docker tests are run separately.
- Ruff lint and formatting checks passed for all **25 Python files**.
- Initial full integration suite: **11 passed** in 120.95 seconds on image `sha256:6a1b6e83e2616bb61d4bb7c8953a56b411fb7eb706a8651a766d09e22f1c7eb9`.
- Review then corrected a boundary case: a drain return value of `True` must be discarded if the stage expires while exiting. A unit regression and an additional real drain-timeout case were added.
- Final affected integration checks: **3 passed, 9 deselected** in 35.88 seconds on rebuilt image `sha256:c1d887ff319820886b24f8214d2c718c2b4aa014fc8e8559fb73cee0bdc028f7`.
- Final inventory: **zero Faultline-owned containers and zero Faultline-owned networks** remained.

There are now twelve integration cases. Eleven passed before the last drain-boundary correction; the three deadline cases, including the newly added twelfth case, passed afterward. Do not describe this as one uninterrupted twelve-test run on the final image. No earlier twenty-trial acceptance matrix was repeated for this increment.

## What was demonstrated

The full integration phase retained the unsafe/correct purchase matrix, actual observer permissions, payload-conflict handling, final-client-timeout success, required-fault coverage, SIGTERM cleanup, and SIGKILL recovery.

The final deadline-specific cases demonstrated:

1. **Observation timeout:** the coordinator interrupted a real Docker exec wait on an observer helper sleeping for 30 seconds. The run became ERROR without a fabricated final snapshot, and cleanup removed the helper's container and the other owned resources.
2. **Drain timeout:** a deliberately stalled drain left the application running during observation. Even though the database contained the correct single debit, quiescence remained uncertain and the run was INCONCLUSIVE. Cleanup then removed the resources.
3. **Cleanup timeout:** a deliberately stalled cleanup exhausted its resource-deletion allowance while real owned resources existed. The ERROR report preserved all six unresolved IDs and the completed passing invariant results. A later scoped cleanup removed the resources, and the original report bytes remained unchanged.

The cleanup stall is a controlled test hook; it is not a claim that the Docker daemon naturally stalled during validation. The observation case waits on a real container process. Both tests exercise the ordinary engine deadline and cleanup paths.

Unit cases also cover invalid bounds/types, effective plan defaults, real signal interruption of blocking waits and CPU-bound verification, existing-alarm preservation, main-thread refusal, total-work clipping, retained partial attempts/fault coverage, deadline-boundary quiescence, unresolved cleanup records, and finalization expiry with lock release.

## Retained evidence and reproduction

Ignored local artifacts:

- `.faultline/deadline-validation/` and `.faultline/deadline-validation-results.xml`: initial full integration phase.
- `.faultline/deadline-final-validation/` and `.faultline/deadline-final-validation-results.xml`: final affected integration phase.

Completed report bundles include deadline diagnostics and stage timing. The cleanup-timeout case also includes a separate recovery receipt; it deliberately preserves the original ERROR report as historical evidence.

After rebuilding the runtime image, run these from Linux/WSL:

```bash
uv run pytest -q tests/test_deadlines.py
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py -k deadline
```

The complete opt-in integration suite remains available through the [README commands](../README.md#verification). The [deadline learning guide](learning/deadlines.md) explains the supported guarantees, timer arithmetic, and remaining limitations. Earlier [purchase](acceptance-results.md) and [recovery](recovery-acceptance-results.md) measurements remain separate historical records.
