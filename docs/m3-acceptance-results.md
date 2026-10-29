# M3 acceptance results (in progress)

This page records measured M3 evidence as each part lands. Targets from
`specs/10-delivery.md` are not results; anything not listed here has not been
demonstrated yet.

## Phase A: strict replay, comparison, and repeat groups (2026-10-29)

Environment: Windows 11 host, Debian WSL2, Docker Engine 28.4.0, runtime image
`faultline-runtime:0.1.0` rebuilt from this revision
(`sha256:4d431ffde6d883474338e86607f5b66296d1c4193402046bcd17cf93438f875d`).

### Unit evidence

`uv run pytest -q`: **315 passed, 40 Docker tests skipped**; `ruff check` and
`ruff format --check` passed.

- AT-015 (honest frequency): `tests/test_groups.py` builds a controlled mixture of
  matching FAIL, unrelated FAIL, PASS, INCONCLUSIVE, an ERROR that also contains the
  target violation, and an interrupted FAIL. It asserts the exact counts, 2/7 matching
  per started trial, 2/4 per conclusive trial, `not_available` for zero denominators,
  and that neither the ERROR nor the interrupted run counts as a reproduction.
- AT-014 rejection paths: `tests/test_replay.py` covers exact plan rebuilds, including
  generated workloads. It also covers modified adapter content hashes, a changed engine
  version, changed or missing image IDs, a kernel change, unsupported plan versions, a
  source without recorded environment identity, and comparison semantic drift (fixture
  hash and invariant version). Each rejection happens before any run is provisioned.

### Real Docker evidence

`FAULTLINE_INTEGRATION=1 uv run pytest -q -s tests/test_replay_integration.py`:
**1 passed in 120.61 s**. One case:

1. `run lost-response.yaml --variant unsafe` against the example project: FAIL.
   The source run had several violation signatures, as expected for an unsafe
   duplicate: duplicate debit, non-atomic purchase, and others. The test selected the
   `duplicate_debit` signature explicitly.
2. `replay <run> --repeat 3 --signature <duplicate_debit>`: 3 started, 3 FAIL,
   **3 matching failures, 3/3 per started and 3/3 per conclusive**, 0 ERROR, cleanup
   complete. Each replay manifest had `origin: replay`, the source run ID and group ID,
   the source plan hash, and an identical platform record (image IDs, kernel, Docker).
   Each `plan.json` was byte-identical to the source plan. Each baseline snapshot
   matched the source baseline digest.
3. `compare <run> --variant correct --repeat 2`: 2 PASS, 0 matching failures. The
   recorded differences were exactly `variant` and `platform.variant`. Both variants
   use the same runtime image, so no image difference was reported.
4. `report <group-id>` returned the persisted replay group. No Faultline-managed
   containers remained.

The first attempt at this test failed on its own assertion: it expected a target to
be selected automatically, but the source run had several signatures. The CLI behaved
as specified and reported no reproduction fraction. The test now passes
`--signature`; the behavior was not changed to fit the test.

## Phase B: dependency-aware reduction (2026-10-29)

Runtime image rebuilt after the fixture changes
(`sha256:2bb2c30b393fa392d48dff06c417ea159cf58226efc0584210543e48401a6901`).

### Unit evidence (AT-017)

`uv run pytest -q`: **328 passed, 42 Docker tests skipped**; lint and format passed.
`tests/test_reduction.py` uses a fake laboratory and clock to cover operation, fault,
barrier, and retry dependency closure. It also covers an unrelated signature that never
satisfies the target, unstable and erroring baselines, an exhausted search
(`no_smaller_reproduction`), and run-budget exhaustion before any improvement
(`insufficient_budget`). Wall-budget exhaustion before validation and failed final
validation are both checked to yield `reduction_unverified`. Interruption persistence,
the explicit signature rule, and byte-for-byte immutability of the source bundle are
covered too. `tests/test_verifier.py` checks per-account and per-product reconciliation
in the new `reduction_market` fixture.

### Real Docker evidence (AT-016)

`FAULTLINE_INTEGRATION=1 uv run pytest -q -s tests/test_replay_integration.py tests/test_reduction_integration.py`:
**2 passed in 901.28 s**. The replay case repeated Phase A's result: 3/3 matching, and a
comparison whose only differences were `variant` and `platform.variant`.

Reduction of `examples/orders/reduction-market.yaml` (20 operations: one lost-response
purchase plus 19 independent noise purchases), unsafe variant, target
`orders.at_most_one_debit` / `duplicate_debit` / `purchase-a`, with
`--max-runs 80 --max-duration-ms 3600000`:

| Measure | Result |
| --- | --- |
| Outcome | `reduction_verified`, search exhausted |
| Objective (operations, faults, attempts) | 20, 1, 21 to **1, 1, 2** (95% fewer operations) |
| Removed | all 19 noise operations; the fault and retry were retained |
| Baseline | 5/5 matching, 0 ERROR |
| Candidates | 18 tried, 6 accepted, 0 invalid |
| Candidate trials | 31: 19 FAIL, 10 PASS, 2 INCONCLUSIVE |
| Final validation | 5/5 matching, 0 ERROR |
| Budget used | 41 trials, 747.6 s wall time, 3.4 MB of artifacts |

The final fault-removal and retry-trim candidates each produced a PASS. This shows that
both the response suppression and the second attempt are needed for the duplicate
debit.

Both INCONCLUSIVE trials had the duplicate's attempt pattern, but the application did
not exit within the 10 s drain budget (`deadline_exceeded` during draining, then
`drain_incomplete`). Their final snapshots were not quiescent, so every invariant
reported UNKNOWN. The reducer correctly treated both trials as non-matching and rejected
those candidates; a later candidate with the same objective was accepted. This was 2 of
41 trials under WSL2 on this machine. It is an observed timing limitation, not a
measured rate for other hosts.

The reduction took 12.5 minutes, longer than the default 10-minute budget. With default
limits it would have stopped early and reported that honestly; the acceptance test
raises the wall budget explicitly.

### Not yet demonstrated

The expanded artifact robustness checks (AT-018), the read-only API (AT-020), and the
React viewer (AT-019) are pending.
