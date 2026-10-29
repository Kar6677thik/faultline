# M3 acceptance results

This page records measured M3 evidence. Targets from `specs/10-delivery.md` are not
results; anything not listed here has not been demonstrated.

| Gate | Evidence |
| --- | --- |
| AT-014 replay fidelity | Phase A unit rejections; real replay/comparison (Phases A, B, final) |
| AT-015 honest frequency | `tests/test_groups.py` controlled mixture |
| AT-016 smaller reproduction | Real 20 to 1 verified reductions (Phase B, final) |
| AT-017 reduction integrity | `tests/test_reduction.py` |
| AT-018 artifact robustness | `tests/test_server.py`, `read_bundle` |
| AT-019 viewer investigation | `web/src/test/viewer.test.tsx` (jsdom) |
| AT-020 local web boundary | `tests/test_server.py`, real `serve` smoke test |

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

## Phase C: read-only API, viewer, and artifact robustness (2026-10-29)

### Unit and viewer evidence

- `uv run pytest -q`: **349 passed, 42 Docker tests skipped**; `ruff check` and
  `ruff format --check` passed.
- AT-020 (`tests/test_server.py`):
  - the token is required for `/api/*` (401); a foreign Host or Origin gets 403;
    POST/PUT/PATCH/DELETE/OPTIONS get 405, with no CORS headers;
  - the page sends a strict CSP and `nosniff`, contains no run data, and loads
    nothing from another origin;
  - artifacts are addressed only by opaque IDs, and both traversal forms return 404;
  - an artifact changed after listing gets 422.
- AT-018 (`tests/test_server.py` and `read_bundle`): round trip; duplicate event IDs,
  missing attempt events, truncated last lines, and future event versions are rejected
  even after the inventory hashes are rewritten; corrupt bundles (422) and unsupported
  versions (409) get distinct responses.
- API contract tests cover money and nanosecond values as decimal strings, stable
  cursor pagination while a new run appears, RUNNING versus INTERRUPTED incomplete
  bundles, event pagination and ETags, and snapshot bounds.
- AT-019 (`web/src/test/viewer.test.tsx`, Vitest + Testing Library + jsdom): **19 passed**.
  - token capture and fragment removal; the reload instruction;
  - the loading, empty, unauthorized, not found, not ready, corrupt, unsupported,
    server error, disconnected, and incomplete states;
  - upstream witness versus client timeout, planned versus observed attempts,
    ERROR with an attached violation, and PASS scope;
  - exact 64-bit values; markup in an artifact shown as text;
  - both group denominators and an unverified reduction labelled as unverified;
  - a keyboard path from the run list through the run and the invariant to both
    duplicate ledger rows.

### Real evidence smoke test

`faultline serve --port 0` was run against fresh real bundles (an unsafe lost-response
run plus a 2-trial strict replay, retained under `.faultline/m3-viewer-smoke`). With
curl:

- `/health` returned 200; the API without a token returned 401, a foreign Host or Origin
  403, and POST 405;
- the run list showed the run and both replays, and the replay group 2/2 per started
  trial;
- the run detail listed four failing invariants; the final ledger had two 200000-paise
  debits, returned as strings; events paginated with `source_monotonic_ns` as a string;
- the SPA route and the hashed JS asset were served, and SIGINT exited with code 0.

The first smoke attempt found a real defect: `serve` printed its URL before the bound
socket was listening, so immediate connections were refused. The fix calls `listen()`
before announcing the URL. The viewer was not walked through manually in a real
browser for this record; its behavior evidence is the jsdom suite plus the API checks
above.

## Final M3 regression (2026-10-29)

After rebuilding `faultline-runtime:0.1.0`
(`sha256:3d8f3293c73d5e440cf27ff4ca0156566d4953dc504c86c3106b16787ce816ae`; counter image
`sha256:b13feb77de50eaadc59e7cdd263ece5ccac10ffb663169849efb5faecdc28f8e`):

`FAULTLINE_INTEGRATION=1 uv run pytest -q -m integration --basetemp=.faultline/m3-full-validation`:
**42 passed in 1292.55 s, in one invocation on the final source** (14:42:45Z to
15:04:22Z). These are the 40 earlier Docker cases plus the replay and reduction cases.
Evidence is retained under `.faultline/m3-full-validation/`. Afterwards, no
Faultline-managed containers or networks remained.

In this pass:

- the replay group again had 3/3 matching failures (3/3 per started trial); the
  comparison had 2/2 PASS with differences `variant` and `platform.variant`;
- the reduction (group `23dd08e5-d2eb-4417-97ca-b389894a2a55`) was again
  `reduction_verified`, going from 20, 1, 21 to **1, 1, 2**. It had 5/5 baseline and
  5/5 validation, 12 candidates (5 accepted), and 22 candidate trials (15 FAIL, 7 PASS,
  0 INCONCLUSIVE). It used **32 trials in 515.4 s**, which happens to fit the default
  50-trial/10-minute budget, unlike Phase B's 41 trials in 747.6 s. The two passes
  differ because outcomes are not deterministic; neither is a performance claim.

### Not demonstrated

No manual real-browser accessibility audit (screen reader, 375 px device) is recorded. The
Phase B reduction temp bundles were rotated away by pytest; their measurements come from
the test output. The final regression's bundles are retained.
