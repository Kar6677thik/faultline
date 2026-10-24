# What happens when Faultline itself crashes?

## The simple explanation

Imagine an experiment running on a table. The person supervising it suddenly leaves. The equipment does not necessarily stop. Before packing up, a replacement supervisor must know that nobody is still using the table, which equipment belongs to this experiment, and what was already written down.

Faultline's coordinator is the supervisor. Docker containers are the equipment. The operating-system lock is the table's key. The run manifest is the equipment list. Cleanup receipts record the packing-up work. New runs also save creation tickets before asking Docker to make equipment, so a lost creation receipt can be [reconciled safely](creation-recovery.md).

## Why containers survive a hard kill

The CLI asks the Docker daemon to create containers. Those containers are managed by Docker, not as ordinary child processes that automatically disappear with the CLI.

SIGINT (usually Ctrl+C) and SIGTERM ask the CLI to stop. Faultline handles them through its ordinary cleanup path. SIGKILL stops a process immediately; Python cannot run its `finally` blocks or write a final report. Docker resources can remain, and the bundle may stop at an intermediate stage.

This is why “we have a finally block” is useful but insufficient recovery design.

## Run recovery

From Linux/WSL, after following the [setup instructions](../../README.md#setup):

```bash
# Recover one known run under the default .faultline output root.
.venv/bin/faultline clean --run <run-uuid>

# Check recorded runs in that root and remove any owned leftovers.
.venv/bin/faultline clean --stale

# Use the same output root that the interrupted experiment used.
.venv/bin/faultline --output /path/to/results --format json clean --stale
```

Use exactly one selector: `--run` or `--stale`. Run IDs come from the CLI output or directory names under `<output>/runs/`. Recovery does not require the runtime images to remain installed, but deleting resources requires the local Docker daemon. An empty stale selection succeeds without Docker.

Use the original output root. New manifests record it in `execution_root`. Copying a bundle elsewhere does not copy the original process's lock, so cleanup refuses moved/copied bundles. Legacy manifests without this record are also refused for automatic recovery. Completed reports still work offline and can be copied for inspection; inspection has no deletion authority. Do not edit ownership metadata to bypass this check.

Do not delete `execution.lock` to bypass a refusal. A running process may still hold the original file open; creating a replacement file would create a different lock and defeat coordination.

## The precise sequence

1. **Acquire the lock.** Cleanup takes the same output-root lock as `run_one`. If another supported operation holds it, cleanup returns exit 2 before contacting Docker.
2. **Read recorded bundles.** `--run` selects one UUID. `--stale` scans at most 1,000 entries directly under `runs/`, including completed bundles. It never recursively hunts for containers or scans Docker by name.
3. **Validate the entire selection.** Require the recorded original execution root to match the selected root. Reject missing/unsafe paths, malformed JSON, duplicate fields, incompatible version/profile, invalid run/nonce UUIDs, invalid resource records, and malformed creation intents. Full resource IDs, allowed roles/kinds, and uniqueness are required. Before deletion, validate any saved recovery bindings against their exact original manifest. A malformed member stops the batch before resource deletion.
4. **Create a receipt.** Write `<output>/cleanup/<cleanup-uuid>.json` with `complete: false`, the selected run identity, and its recorded resources. If this cannot be written, do not start deleting that run's resources.
5. **Reconcile pending creations.** For an intent without an ID, inspect only its exact recorded name. Require all ownership and creation-token/kind/role labels, then save the full immutable ID under `cleanup/bindings/<run-uuid>.json` before any deletion. A missing or mismatched object remains unresolved. Original artifacts are not modified.
6. **Recheck and delete in order.** Fetch each recorded or durably reconciled ID, reload attributes, and verify ownership immediately before mutation. New resources also require matching creation labels and name. Remove containers before networks. A previously bound ID that is absent is successfully cleaned; an absent unresolved intent is still uncertain. Labels or names alone never grant ownership.
7. **Finish the receipt.** Save completion time, `complete`, reconciled IDs, and any unresolved IDs or intents. Release the lock only after the recovery work and receipt writes finish.

There is one receipt per selected run per invocation, including runs whose resources are already absent. A second cleanup attempt creates a new receipt and preserves the first. It reuses existing ID bindings instead of resolving the same name again. Preserve `cleanup/bindings/` alongside the run manifests. The current receipt reports unresolved resources but does not provide a detailed Docker failure reason for each one.

## What “stale” means here

It means no supported run/cleanup operation currently holds this output root's kernel lock. It does not mean “older than five minutes,” “the manifest says completed,” or “a recorded PID disappeared.” No PID or age inference is used.

The lock is conservative. A live run blocks cleanup of all runs in that root, even a different old run. Different output roots have separate locks and unique run identities. `--repeat` locks each trial separately, so another command can acquire the root between trials. Coordinate repeat jobs accordingly; group-wide scheduling is not implemented.

This guarantee assumes a trusted local workspace and callers using the supported CLI or `run_one`. It is not protection against a hostile local user replacing files during cleanup, a malicious Docker administrator, or code bypassing the engine and using internal environment helpers directly.

## How to interpret the outcome

| Exit code | Meaning for cleanup | Next step |
| --- | --- | --- |
| `0` | All selected recorded resources were removed or already absent | Read the receipt if needed; original experiment results are unchanged |
| `2` | Invalid selector/manifest, unsupported environment, or an active lock | Resolve the input or let the active operation finish; use `--run` to select a valid bundle if a stale batch contains an invalid one |
| `4` | Operational failure, incomplete deletion, or a receipt-write failure after recovery began | Retain the evidence, investigate the daemon/permissions/disk problem, then retry scoped cleanup |

An old experiment may say cleanup failed while a later receipt says cleanup succeeded. Both are true at their recorded times. `faultline report` continues to show the original completed report. It does not silently combine later receipts into a changed verdict. A hard-killed run without a completed report remains unreadable as a completed experiment, even after its containers are gone.

## What the tests establish

The real integration test starts a separate protected environment, starts a child CLI experiment, waits until a response has been suppressed, and tries cleanup while that child is still alive. It must refuse. The test then sends SIGKILL, verifies actual leftover containers exist, and runs stale cleanup twice.

It checks that the recorded leftovers disappeared, the protected environment still works, two cleanup receipts exist, every original experiment file is byte-for-byte unchanged, and reporting the unfinished experiment still fails. The test has its own scoped fallback cleanup so an assertion failure does not deliberately leave test resources behind.

Unit tests separately cover wrong versions/IDs, duplicate records/JSON fields, oversize manifests, symlinks, FIFOs, lock contention, copied bundles bypassing the original lock, bounded scans, ownership mismatch, and inability to write an initial receipt. A FIFO is a pipe disguised as a filesystem entry; opening one without care can hang while waiting for another process. Nonblocking open plus regular-file validation prevents that trap here.

See [test_recovery.py](../../tests/test_recovery.py), [test_integration.py](../../tests/test_integration.py), and [recovery acceptance results](../recovery-acceptance-results.md).

## Remaining limitations

New runs can recover a resource created before its returned ID was recorded, using the prewritten creation intent and all matching labels. Legacy runs without intents retain the earlier limitation. An intent whose object is absent remains unresolved: the request may still finish later, or may never have reached Docker. There is no automatic abandonment command. See the [creation recovery guide](creation-recovery.md) and its [measured results](../creation-recovery-acceptance-results.md).

Docker requests have individual timeouts and the stale scan is bounded, but a configurable deadline for the whole recovery command is not implemented. A daemon failure can prevent cleanup; a disk failure can prevent a receipt update. An initial incomplete receipt is intentionally conservative and can be followed by another attempt.

There is no automatic cleanup during `report`, no global prune, and no artifact deletion command. These decisions preserve evidence and keep read-only inspection free of resource mutations.
