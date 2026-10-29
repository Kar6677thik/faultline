# Calibrating the judge: known bugs and two correct implementations

How do you know a smoke alarm works? You hold a match under it, and you also check that
it stays quiet while you make toast. A checker that never fires is useless, and so is
one that always fires.

Faultline is calibrated the same way:

- **Known-bad variants** of the order service each contain exactly one deliberate bug.
  The matching experiment must catch it, using database evidence.
- **Two independently written correct variants** must pass every applicable experiment.
  If only one correct version existed, a check could accidentally depend on how that
  version happens to work, rather than on the business rule.

## The variants

All variants live in the reference application and are selected with `--variant`. The
verifier is never told which one ran.

| Variant | What it does | Defect |
| --- | --- | --- |
| `correct` (correct-A) | locks the account and product rows, relies on unique indexes, one transaction | none |
| `correct_b` | `SERIALIZABLE` transactions retried up to 8 times, no row locks, no unique indexes, replays rebuilt from the stored order | none |
| `unsafe` | skips all key and purchase-identity checks | missing idempotency |
| `racy_idempotency` | checks identity in its own transaction *before* taking any lock | check-then-insert race |
| `non_atomic` | commits the debit, then writes the order in a second transaction | non-atomic side effects |
| `lost_update` | reads the balance without a lock, writes back an absolute value | lost balance update |
| `payload_unchecked` | returns the stored result for a reused key without comparing payloads | idempotency payload mismatch |
| `missing_outbox` | never writes the confirmation outbox intent | missing confirmation intent |

Each bug variant removes **one** safeguard from correct-A's code
([`orders.py`](../../src/faultline/orders.py), the `DEFECTS` table). None returns
fabricated failure data: each produces its defect through real PostgreSQL behavior.

Correct-B ([`orders_b.py`](../../src/faultline/orders_b.py)) is a separate module with a
different technique. With serializable isolation, PostgreSQL aborts any transaction
whose interleaving could not have happened one at a time, and B simply tries again.
That is why B can safely write absolute balances, which is exactly the move that breaks
`lost_update` under the default isolation level. The comparison makes a good exercise:
the same line of SQL is correct in one place and a bug in the other, because of the
isolation level around it.

## Making real races observable

Races need overlapping work. Each purchase takes only milliseconds, so requests released
together usually finish one after another, and `racy_idempotency` and `lost_update`
passed their first probes. That does not mean the bugs are absent. The experiments
`concurrent-duplicate-slow.yaml` and `concurrent-slow.yaml` add a declared
**proxy-latency fault** (100 ms per database response). Each transaction then lasts long
enough for the requests to overlap. The variant code is unchanged; only the network
conditions differ, and they are recorded like any other fault.

`restart-during-purchase.yaml` combines that latency with an application crash placed
2.7 s into the run. Under latency a purchase takes about 2.7 s, and `non_atomic`
commits its debit about 2 s in, so the crash usually lands between the two commits.
The offset came from measured timelines, not guesswork. A crash elsewhere simply means
that trial does not detect the bug, and calibration reports the frequency instead of
hiding it. Both correct variants must still finish the purchase exactly once after the
restart, using up to five patient retries.

## The corpus file and the calibration tool

[`examples/orders/corpus.json`](../../examples/orders/corpus.json) pairs each defect with
its scenario and **target violation**: invariant, violation code, and logical
purchase. It also lists the scenarios every correct variant must pass.

```bash
python -m faultline.calibration examples/orders/corpus.json --trials 20 --output .faultline/calibration
```

[`calibration.py`](../../src/faultline/calibration.py) runs every trial as a separate
`faultline run` with fresh containers, so an ERROR never skips the remaining trials. It
writes `calibration.json` (every trial, verdict, violations, and timing) and
`calibration.md` (a table). The rules:

- a defect is **detected** only by a *completed FAIL containing the target*. An ERROR
  that also shows the violation does not count;
- the release gate (AT-021) needs at least one detection in 20 trials. `--strict`, for
  CI, needs a detection in every trial;
- a correct variant must PASS **every** trial. One INCONCLUSIVE blocks the gate until
  explained.

These numbers describe a small, deliberately chosen corpus on one machine. They are not
a general detection rate for Faultline.

## Tests

[`tests/test_calibration.py`](../../tests/test_calibration.py) checks the counting rules,
lenient versus strict criteria, that every corpus pair validates against the adapter,
and that the application and adapter agree on the variant list. It also has a static
guard: correct-B uses serializable isolation, has no `FOR UPDATE`, and does not reuse
A's functions.
