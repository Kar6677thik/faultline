# Reduction: finding a smaller failing experiment

Imagine a cake recipe with twenty ingredients that collapses in the oven. You suspect
only a few ingredients matter. You could remove half of them and bake again. If it still
collapses *the same way*, those ingredients were irrelevant. If it doesn't, put them back
and try removing a different half. Repeat with smaller groups until removing anything
else stops the collapse.

That is **delta debugging**, and it is what `faultline reduce` does with a failing plan.
Every "bake" is a real, fresh Docker experiment, so it costs time. That is why the
search has hard budgets and refuses to promise the smallest possible recipe.

## What can be removed

The reducer deletes three kinds of unit, in this order:

1. **Logical operations**, each with all its attempts.
2. **Fault rules**.
3. **Retry suffixes**: the last potential attempt of an operation, one at a time.

It never changes request payloads, amounts, timings, the fixture, or the invariant
list. A smaller plan is therefore still the same kind of business experiment.

## Removing something removes what depends on it

Plans have dependencies, and a deletion must not leave a dangling reference.
`remove()` in [`reduction.py`](../../src/faultline/reduction.py) repeats these rules until
nothing changes:

- an operation that waits `after` a removed operation is removed;
- a fault triggered by a removed operation is removed;
- an operation gated by a removed fault (`after_faults`) is removed; v1 never rewrites
  a gate into an "equivalent" ungated plan;
- a barrier loses removed members and disappears when it has none left.

`trim_retry()` removes an operation's last attempt and its backoff, plus any fault that
targeted that attempt. Each candidate is then validated by the ordinary scenario model
and the adapter, just like user input. An invalid candidate costs planning time but no
trial budget.

## Deciding whether a candidate still fails "the same way"

The target is one **violation signature** (see the [replay guide](replay.md)). An unsafe
duplicate usually fails several invariants at once, so `reduce` requires `--signature`
whenever there is more than one. It prints the choices and exits with code 2 instead
of guessing. A run that fails with a *different* signature does not count, even
though it is also a failure.

The rules, in order:

1. **Baseline:** run the original plan five times. At least four must reproduce the
   target with no ERROR. Otherwise the result is `baseline_unstable` and nothing is
   deleted.
2. **Candidates:** up to three fresh trials each. All three must be completed FAIL runs
   containing the target. The search stops trying a candidate at the first
   non-matching trial (early rejection). The record keeps the actual number of trials.
3. **Objective:** a candidate must strictly improve the tuple (operations, faults,
   potential attempts), compared in that order.
4. **Final validation:** five more fresh runs of the best candidate. Four matching
   with no ERROR earns `reduction_verified`. Otherwise the result is
   `reduction_unverified`, and the original plus the best candidate are both kept.

An ERROR run that happens to contain the violation is never a reproduction, because the
harness itself failed.

## Budgets

Defaults are 50 trials or 10 minutes, whichever comes first. You can raise them to 500
trials or 60 minutes. The clock includes provisioning and cleanup, because those are
part of each trial. Before every candidate trial the reducer checks that five
validation trials still fit, measured in trial count and in wall time (using the
slowest observed trial). Repeat and reduction groups also share a 1 GiB artifact
budget.

When the budget runs out:

- before any candidate was accepted: `insufficient_budget` (exit 3). Running out is not
  evidence that no smaller plan exists;
- after a candidate was accepted: the best candidate is validated if room remains,
  otherwise `reduction_unverified` (exit 3);
- if the search finishes without accepting anything: `no_smaller_reproduction`
  (exit 0).

A 20-operation plan where nothing can be removed needs roughly 50 single-trial
candidates, so the default budget will often end with `insufficient_budget`. That is
an honest result, not a bug.

## Where the evidence goes

Everything is written under the reduction group and rewritten after every trial:

- `<output>/groups/<group-id>.json` holds the target, budget use, the baseline and
  validation counts, and every candidate (phase, removed units, objective, validity,
  each trial, and the reason it was kept or rejected). It also holds the best
  candidate, the outcome, and why the search stopped.
- `<output>/groups/<group-id>/candidates/NNNN.json` holds each valid candidate plan.
- `<output>/groups/<group-id>/reduced-scenario.json` holds the best scenario, ready
  to become a regression test once the application is fixed.
- Every trial is an ordinary run bundle with `origin: reduction`, the group ID, and its
  purpose (`baseline`, `candidate-N`, or `validation`).

The source bundle is only read, never modified.

## The acceptance fixture

`reduction_market` ([`examples/orders/reduction-market.yaml`](../../examples/orders/reduction-market.yaml))
extends the standard wallet with 19 buyers (`buyer-01` to `buyer-19`, ₹10,000 each)
and `sku-noise` (₹100, stock 100). One lost-response purchase by `buyer-a` duplicates a
debit. The other 19 purchases are real, successful, independent noise.

Supporting a second fixture changed three places, each kept independent:

- the adapter contract gained an optional `fixtures` map. Adapters that declare extra
  fixtures receive `prepare(environment, variant, fixture=name)`; existing adapters keep
  the two-argument call;
- the reference application seeds the chosen fixture and records its name in a table the
  application role cannot read or change ([`orders.py`](../../src/faultline/orders.py));
- the verifier keeps its own expected opening rows ([`verifier.py`](../../src/faultline/verifier.py)).
  It reconciles every account and product separately, so charging the wrong buyer is
  caught even when totals agree.

## Try it

```bash
faultline run examples/orders/reduction-market.yaml --variant unsafe
faultline --format json report <run-id>      # find the duplicate_debit signature
faultline reduce <run-id> --signature <sig> --max-runs 80 --max-duration-ms 3600000
faultline report <group-id>
```

Predict first: which operations must survive, and why can't the fault or the retry be
removed?

## Tests

- [`tests/test_reduction.py`](../../tests/test_reduction.py) uses a fake laboratory and
  clock (AT-017). It covers a 20→1 reduction with verified validation and an unchanged
  source bundle; dependency closure (operations, faults, barriers, retry suffixes); an
  unrelated signature that never satisfies the target; unstable and erroring
  baselines; run-budget and wall-budget exhaustion; failed final validation;
  interruption; and the explicit-signature CLI rule.
- [`tests/test_verifier.py`](../../tests/test_verifier.py) checks that the multi-buyer
  fixture reconciles independent purchases and catches a duplicate and a mis-charged
  account.
- [`tests/test_reduction_integration.py`](../../tests/test_reduction_integration.py)
  runs the real AT-016 reduction in Docker.

## Limits

- The search is greedy and budgeted. It may not reach even a one-deletion minimum, so
  reports say "smaller reproduction found", never "minimal".
- Outcomes are not deterministic; only candidate selection is.
- Request values, timings, and fixture size are not shrunk in v1.
- Adapter-declared semantic prerequisites beyond what `validate` rejects are not
  modelled; such a candidate can fail validation or simply not reproduce.
