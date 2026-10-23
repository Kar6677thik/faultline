# Recovery increment: measured results

Measured on 2026-10-23 in Debian WSL2 with the local Docker Desktop Linux x86-64 engine. This validates recorded-resource cleanup after coordinator termination; it does not complete Milestone 2 or validate recovery from a machine power failure.

The full suite used runtime image `sha256:a541505ac2c41d080d3efff8327cec6bdef9d3c7baa79ee8c9cacf882a4e0f7a`. Review then identified copied bundles bypassing the original output lock, so manifests gained `execution_root` and recovery began requiring that root. The affected real SIGKILL recovery test was rerun on rebuilt image `sha256:02b1ee15dc0e758a6860b5bdd6fce83d80fb02456061045204ddd697152c0127`. Dependency versions remain locked in `uv.lock`.

## Checks and results

- Final `uv run pytest -q`: **86 passed, 9 skipped** in 1.82 seconds. Docker integration is explicitly opt-in.
- Ruff lint passed; formatting checked all **23 Python files**.
- Opt-in integration suite: **9 passed** in 98.14 seconds, with no warnings reported.
- After the final original-root safeguard: affected integration case **1 passed, 8 deselected** in 21.12 seconds, with no warnings reported. The full nine cases were not repeated after this final change.
- Post-suite label-filtered Docker inventory: **zero Faultline-owned containers and zero Faultline-owned networks**.

The integration suite retained the four scenario/variant regressions: both ordinary purchases passed; unsafe response suppression produced the expected duplicate-debit FAIL; corrected response suppression passed. It also retained isolation, observer permissions, payload-conflict, final-timeout, missed-fault, and SIGTERM checks.

The new SIGKILL test confirmed:

1. Cleanup refused the output root while a real child CLI held the run lock.
2. SIGKILL left actual recorded Docker resources behind.
3. `clean --stale` removed those resources and succeeded again when repeated.
4. Both cleanup attempts produced separate receipts.
5. Original experiment files were byte-for-byte unchanged; no completed report was invented.
6. A separate environment outside the selected root remained owned and usable.

Unit tests additionally cover malformed inventories, wrong versions/profiles/UUIDs, names and short IDs, duplicate fields/roles, oversized files, symlinks, FIFOs, bounded scans, direct-engine lock contention, copied bundles bypassing the original lock, missing original-root metadata, label mismatch, initial receipt-write failure, and operational exit codes.

## Evidence and reproduction

Local ignored evidence is retained under `.faultline/recovery-validation/`. The pytest machine-readable result is `.faultline/recovery-validation-results.xml`. The hard-kill test stores the interrupted experiment and its cleanup receipts under its `interrupted/` subdirectory and the protected environment's ownership record under `protected/`.

The final targeted rerun is retained under `.faultline/recovery-root-validation/` with `.faultline/recovery-root-validation-results.xml`. Earlier phase bundles without original-root metadata remain preserved for inspection; automatic recovery now refuses those legacy manifests.

After building the runtime image, the integration suite can be rerun inside Linux/WSL with:

```bash
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py
```

The earlier twenty-trial measurements remain in [initial acceptance results](acceptance-results.md). They were not rerun for this increment and should not be described as measurements of the new revision. This increment ran one fresh trial per scenario/variant pair plus the five other integration cases.

## Limits

These checks cover a hard-killed coordinator after resource IDs were persisted. They do not close the gap between Docker resource creation and ownership-record persistence. Recovery still depends on a functioning local daemon, trustworthy local files, and matching resource labels. It has bounded per-call operations and input scans, but no configurable total cleanup-command deadline. See the [recovery explanation](learning/recovery.md) and [decision log](learning/decisions.md) for the reasons and tradeoffs.
