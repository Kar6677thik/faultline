# Recovering a resource when its creation receipt is lost

Imagine ordering a labeled box from a warehouse. The warehouse makes it, but your connection breaks before you write down its serial number. You cannot assume the box was never made. You also cannot take an unrelated box just because its name looks familiar.

Faultline has the same problem with Docker. A create request can succeed even if the coordinator dies before recording the returned container or network ID. The earlier recovery implementation could remove only IDs already saved in the manifest. This increment adds a deliberately narrow way to recover the missing serial number.

## Write the request down before sending it

Before each create call, [Environment.begin_creation](../../src/faultline/environment.py) saves a **creation intent** in `manifest.json`. It contains:

| Field | Meaning |
| --- | --- |
| `kind` | Container or network |
| `role` | Its supported job: application, database, observer, worker, client network, or storage network |
| `name` | The exact generated `fl-<run UUID>-<role>` name |
| `token` | A fresh UUID identifying this particular creation request |

Think of the token as a numbered claim ticket. Each resource also receives the existing managed/project/run/ownership-nonce labels, plus `io.faultline.creation`, `io.faultline.kind`, and `io.faultline.role`. A name without the recorded ticket and all matching labels is insufficient.

The manifest write uses the existing atomic file replacement and file `fsync`. Docker creation happens only after the save returns successfully. An ordinary save failure prevents the call and restores the previous in-memory intent list. A hard kill after the save can leave a pending intent even if Docker never received a request; recovery treats that honestly as uncertainty.

When Docker returns normally, Faultline records the full immutable ID before starting the container or using it as a mutation target. Each supported role can have only one creation intent. The engine never automatically retries a create request using another identity.

## Reconciliation means finding the missing serial number

When cleanup encounters an intent whose kind/role has no recorded ID, it:

1. Asks Docker to inspect the **exact recorded name**. It does not list the daemon looking for matching prefixes or broadly collect resources by label.
2. Reloads the object and checks its exact name, the managed/project/run/nonce labels, and the intent's creation token/kind/role labels.
3. Validates that its returned ID is a full 64-character hexadecimal Docker ID and does not conflict with another recorded ID or role.
4. Persists the new ID before permitting mutation.
5. Fetches by that immutable ID, rechecks ownership, and deletes through the ordinary cleanup path.

If Docker cannot resolve the name, returns ambiguous/error results, or any ownership check fails, the intent remains unresolved. Faultline does not try a broader lookup. A foreign object with the same name is left untouched.

For ordinary cleanup while the run is still active, the recovered ID is saved in its manifest. For later `clean`, the original experiment files must remain unchanged, so the ID is written to a separate file:

```text
<output>/cleanup/bindings/<run-uuid>.json
```

This **binding record** connects the previously saved intent to its immutable ID. It includes a schema version, run UUID, ownership nonce, and a hash of the entire original manifest. It stores only IDs/kinds/roles; the exact intents remain in the original manifest. It is created only when a new binding is needed. Cleanup receipts contain its relative path and the reconciled records.

All saved binding records for a selected cleanup batch are validated before deleting any resource. A modified original manifest, malformed binding, conflicting ID/role, symlink, or record without a corresponding intent is refused. The original execution-root lock and confined file reads still apply. These records are part of recovery state: preserve them alongside the manifests and receipts.

## Why the binding must survive another crash

Suppose cleanup finds container A, records its ID, deletes it, and crashes before finishing its receipt. On retry, it must use A's saved ID. If A is absent, cleanup can finish. It must not look up the old name and accidentally select a replacement container B.

That is why the binding record is written **before** deletion and reused on every later attempt. A write failure grants no mutation permission. Removing or editing recovery records to bypass an error is outside the supported workflow. This is not a security boundary against a hostile Docker administrator or concurrent hostile filesystem changes.

The binding file may contain only some of a run's pending intents if cleanup itself was interrupted. A later invocation retains those IDs and may reconcile the remaining intents. Original experiment artifacts and verdicts are never rewritten by `clean`.

## An absent pending object is not proof of cleanup

There is an important difference between these cases:

| Observation | Cleanup interpretation |
| --- | --- |
| A previously recorded immutable ID no longer exists | Successfully absent |
| An intent has no ID and its name is currently absent | Unresolved creation |
| Exact name exists with wrong labels | Refuse ownership |
| Exact name and every label match, and the ID is persisted | Eligible for ordinary ID-based cleanup |

Why not treat the second row as success? A timed-out create request may still be running in Docker. The object could appear after the check. Reporting success at that moment would hide a possible leftover.

If it later appears with the recorded identity, another `clean --run` can reconcile it. If the coordinator died after writing intent but before sending the request, the intent may stay unresolved indefinitely even though nothing was created. There is no automatic "forget this intent" command in this increment. A definitive daemon request-status protocol or explicit reviewed abandonment policy would be separate work. This trades convenience for an honest cleanup result.

`remaining_resources` may therefore contain either a bound `{kind, role, id}` record or an unbound `{kind, role, name, token}` intent. Cleanup remains incomplete and the CLI exits 4 while either kind is unresolved. A cleanup failure does not turn into a business FAIL; available business findings are retained separately.

## What stays compatible

- The user commands are still `clean --run UUID` and `clean --stale`; no new targeting option exists.
- Old manifests without `creation_intents` continue to use only their recorded IDs. The tool cannot reconstruct missing legacy creation records.
- Completed reports remain readable offline. Reporting does not consult bindings or load adapters.
- Same-root locking prevents recovery from acting on a live supported experiment. Copied bundles cannot substitute a different lock.
- Known IDs are never rebound by name, even when an intent is present.
- The current inventory remains bounded to six resources and six intents in the four-container/two-network topology.

## Follow the source and tests

| Responsibility | Source |
| --- | --- |
| Strict ID/role/intent contracts shared by creation and recovery | [ownership.py](../../src/faultline/ownership.py) |
| Initial manifest intent inventory and atomic saves | [artifacts.py](../../src/faultline/artifacts.py) |
| Save intent, attach labels, reconcile, check ownership, delete | [environment.py](../../src/faultline/environment.py) |
| Validate original records, persist/reuse recovery bindings, preserve evidence | [recovery.py](../../src/faultline/recovery.py) |
| Write-before-create/delete, wrong ownership, missing objects, journal failures and reuse | [test_creation_recovery.py](../../tests/test_creation_recovery.py) |
| Real process deaths between creation and recording | `test_sigkill_between_docker_creation_and_id_recording` in [test_integration.py](../../tests/test_integration.py) |

The real tests pause a child immediately after Docker has returned a new network or container, before `Environment.record` saves the ID. A test-only marker lets the parent know when to send SIGKILL; recovery has no access to that marker. The parent confirms the ID is missing from the manifest, verifies live cleanup is refused, kills the child, and invokes normal cleanup twice. It checks the recovered ID, absence of owned leftovers, and byte-for-byte preservation of every original experiment file.

See [acceptance measurements](../creation-recovery-acceptance-results.md) for observed outcomes. These tests exercise coordinator process death, not machine power loss. Atomic file replacement plus file `fsync` does not claim guaranteed storage durability across all OS, Docker, or hardware failures. Whole-command recovery deadlines and per-resource Docker error explanations remain deferred; normal run cleanup still uses its existing stage budget.

## A related interruption bug found during validation

The full regression suite interrupted an event write after its bytes reached disk but before its in-memory sequence counter advanced. The cleanup event reused the same number, so offline report validation correctly rejected the history. Resource cleanup itself succeeded.

Think of writing line four and being interrupted before moving your bookmark. The next writer must check whether line four already exists. `Bundle.event` now checks only the attempted append after an exception: complete matching bytes advance the counter, no bytes leave it unchanged, and partial/unreadable bytes prevent further appending and completed-report finalization. It re-raises the original interruption. It never deletes or invents evidence to make the report pass.

This adds no new event format and does not block cancellation signals across disk writes. [test_artifacts.py](../../tests/test_artifacts.py) tests interruption after a real `fsync` and preservation of a partial write. Decision L-036 records the alternatives and limits. A partial bundle still retains ownership information for scoped cleanup.
