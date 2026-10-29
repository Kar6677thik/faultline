# A second business domain: transfers and the instrumented checkpoint

A checker that has only ever judged one kind of business might quietly depend on that
business's shape: one wallet, purchases, an outbox. The transfer domain is the test of
that: different tables, different rules, and the same engine.

Moving ₹2,500 from account A to account B must do two things: take money out of A and
put it into B. If the program crashes after the first step but before the second, money
disappears. That is the **non-atomic transfer** defect.

## What is in the domain

| Piece | File | Role |
| --- | --- | --- |
| Application | [`transfers.py`](../../src/faultline/transfers.py) | `POST /transfers`; variants `correct` and `non_atomic` |
| Store helper | [`transfer_store.py`](../../src/faultline/transfer_store.py) | runs in the observer: schema, the `two_accounts` fixture, read-only snapshots |
| Verifier | [`transfer_verifier.py`](../../src/faultline/transfer_verifier.py) | four invariants, its own expected opening balances |
| Adapter | [`transfer_adapter.py`](../../src/faultline/transfer_adapter.py) | declares capabilities, validates scenarios, wires the hooks |
| Example | [`examples/transfers/`](../../examples/transfers) | image, project manifest, six scenarios, corpus |

None of these import order code. The adapter is selected in the project manifest with
`entry_point: bundled_transfers`. The engine changes were generic ones (the checkpoint
trigger and a registry of bundled adapters), not transfer logic.

Invariants (spec 06):

- `transfers.conservation`: each account equals its opening balance plus credits
  minus debits; the total is unchanged; nothing is negative.
- `transfers.atomicity`: a committed transfer has exactly one matching debit and one
  matching credit; rejected transfers have no effects; unknown rows are reported.
- `transfers.idempotency_contract`: every 201 response equals the single stored
  transfer, and expected rejections carry the right status and code.
- `transfers.completed_transfer`: each expected transfer committed exactly once.

## Why a crash needs a checkpoint here

The `correct` variant does everything in one transaction, with both account rows locked
in a fixed order so that opposite transfers cannot deadlock. `non_atomic` commits the
debit, then does the credit in a second transaction. The gap between the two commits
lasts microseconds. Hitting it with a timed crash would be luck, and the order
domain's latency trick would only make it less unlikely.

So spec 05 allows one explicit **test instrumentation** point: the
`transfer.debit_committed` checkpoint.

1. A scenario declares a `process_restart` whose trigger is
   `{event: adapter_checkpoint, checkpoint: transfer.debit_committed, operation_id, attempt}`.
2. The engine enables the checkpoint in the application (`FL_CHECKPOINTS`) only for
   plans that use it.
3. When the bound attempt is in flight, the fault controller asks the worker to poll
   `GET /_faultline/checkpoints` on the client network. The application never touches
   the control network or Docker.
4. The non-atomic handler, after its debit commits, records
   `{checkpoint, attempt_id, transfer_id}` and pauses for up to 5 seconds. The attempt
   is identified by the `X-Faultline-Attempt-Id` header the worker already sends.
5. The controller records an `adapter_checkpoint` event, then SIGKILLs the application,
   waits, restarts it, and waits for readiness.
6. The client's retry finds the stored transfer and gets a 201. The ledger still has a
   debit with no credit, and the verifier finds it **from raw rows**. The checkpoint
   message only authorized the crash; it is never evidence of a violation.

Capability gating keeps this honest. The adapter declares
`checkpoints: {"transfer.debit_committed": ["non_atomic"]}`. The atomic variant has no
partial state to pause at, so validating the checkpoint scenario with `--variant correct`
fails before anything starts. A checkpoint scenario can never count as a "passing" test
for a correct variant, and the order adapter, which declares no checkpoints, rejects
such triggers entirely.

If the attempt finishes without reaching the checkpoint, the controller stops watching.
The required fault stays unapplied, and the run is INCONCLUSIVE rather than PASS.

## Try it

```bash
docker build -t faultline-transfers:0.1.0 examples/transfers
cd examples/transfers
faultline run checkpoint-restart.yaml --variant non_atomic   # FAIL: one-sided transfer
faultline run checkpoint-restart.yaml --variant correct      # exit 2: checkpoint unsupported
faultline run process-restart.yaml --variant correct         # PASS
```

## Tests

[`tests/test_transfers.py`](../../tests/test_transfers.py) checks hand-audited states:

- a correct transfer passes; a one-sided debit, a duplicate debit, a misleading response,
  unrequested rows, and a rejection with effects are each caught;
- non-quiescent evidence is UNKNOWN, and a wrong opening fixture is an evidence error;
- the checkpoint trigger contract, variant gating, and that the plan records its
  checkpoints;
- the controller polls only while the bound attempt is admitted, ignores other
  attempts' checkpoints, and records the acknowledgment before the crash;
- a missed checkpoint leaves the fault unapplied.

## Limits

- Instrumentation is a test seam inside the application. It shows what happens *if*
  a crash lands after the debit; it does not measure how often that happens unprompted.
- The pause is bounded (5 s). Under extreme load the crash could arrive after the
  handler resumes, and that trial would simply not detect the defect.
- There is no independent correct-B for transfers; the two-correct-variant gate
  (AT-022) applies to the order domain.
