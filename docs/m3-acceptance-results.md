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

### Not yet demonstrated

Reduction (AT-016/AT-017), the expanded artifact robustness checks (AT-018), the
read-only API (AT-020), and the React viewer (AT-019) are pending.
