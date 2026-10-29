# Replay, comparison, and honest repeat counts

Imagine you baked a cake that collapsed. To find out whether it collapses *every*
time, you bake it again with the same recipe, the same oven, and the same flour.
If you change the flour, you are no longer repeating the experiment; you are comparing
two flours. Both are useful, but you must say which one you did.

Faultline has the same distinction:

- **Strict replay** runs the exact saved plan again, in fresh containers, with the same
  images, adapter code, engine version, and platform. If anything differs, replay
  refuses before creating any resource.
- **Comparison** runs the saved plan against a variant you choose explicitly (for
  example, the fixed application). It records every difference instead of hiding it,
  and it is never labelled as a replay.

Neither promises the same thread or transaction timing. Docker and PostgreSQL still
schedule work their own way, so a replay is a fresh observation of the same recipe,
not a recording being played back.

## What is replayed

A run's `plan.json` already contains the concrete workload: generated values,
request body bytes, retry rules, faults, barriers, and limits. Replay never
re-runs a generator. `Scenario.from_plan` in
[`models.py`](../../src/faultline/models.py) rebuilds the scenario from the saved
`scenario` section and keeps the generator provenance (the generator ID, version, and
algorithm) so the replayed plan is byte-for-byte the same plan with the same hash.

It refuses plans with an unsupported plan, fault-semantics, or canonicalization
version, and plans that still contain unexpanded generators.

## The strict checks, in order

All of these live in [`replay.py`](../../src/faultline/replay.py):

1. `load_source` verifies the bundle's artifact hashes with `read_bundle`, checks that
   `plan.json` matches the recorded plan hash, and requires the recorded platform and
   adapter identity. A run that failed before those were recorded cannot be replayed.
   A project run uses the **saved** `project.json`, checked against its hash, not the
   project file that happens to be in your current directory.
2. `replay_scenario` loads the same adapter, rebuilds the plan, and compares three
   things: the plan hash, the engine version, and the adapter package/version/content
   hash. The bundled adapter's content hash covers all Faultline source files, so an
   edited engine is a different engine.
3. `check_platform` runs after the normal Docker preflight. It compares the whole
   recorded platform record: OS, architecture, kernel, Docker version, every resolved
   image ID, the project configuration hash, and the variant. A rebuilt image has a
   new ID and is rejected; a missing image fails preflight before this step.

A rejection returns exit code 2 with each differing field, for example
`platform.images.runtime`. Nothing is provisioned and no group is created.

## Comparison

`faultline compare RUN --variant correct` uses the *current* project (if any) and the
chosen variant. Some differences make a before/after claim meaningless, so they reject
the comparison: different request bodies, different invariant versions, or a different
fixture (profile, version, or baseline digest). Everything else is recorded in the
group and in each new run's manifest under `comparison.differences`. Typical
differences are `variant`, `platform.variant`, and `platform.images.application`.

## Groups and honest counting

Every replay or comparison, and every `run --repeat N` above one, creates a group
record in `<output>/groups/<group-id>.json` ([`groups.py`](../../src/faultline/groups.py)).
The record is rewritten after each trial, so an interrupted group still shows what
happened.

A group counts, for the scheduled trials:

- `started`, `completed`, `interrupted`, and `unstarted`;
- verdict counts for PASS, FAIL, INCONCLUSIVE, and ERROR;
- `matching_failures`: completed FAIL runs that contain the **target signature**;
- `other_failures`: FAIL runs without it.

A *signature* is the adapter's stable fingerprint of a violation: invariant, version,
violation code, and logical business identity, without volatile row IDs or times. If
the source run has exactly one signature, it becomes the target automatically. With
several, pass `--signature`; otherwise no reproduction fraction is reported.

Two fractions are reported:

- `per_started`: matching failures divided by every started trial;
- `per_conclusive`: matching failures divided by completed PASS or FAIL trials.

When a denominator is zero the ratio is `not_available`, not zero and not one.

Some rules exist specifically so that counts cannot flatter the result:

- An ERROR run that also observed the violation is **not** a reproduction; the harness
  failed, so the evidence is not trustworthy.
- An interrupted run is never a matching failure or a conclusive run.
- A group's verdict uses ERROR > FAIL > INCONCLUSIVE > PASS precedence. Mixed results
  are never PASS.
- These numbers describe the trials that ran. They are not a probability for all
  possible schedules.

## Try it

```bash
faultline --project examples/orders/faultline.project.yaml run lost-response.yaml --variant unsafe
faultline --project examples/orders/faultline.project.yaml replay <run-id> --repeat 3
faultline --project examples/orders/faultline.project.yaml compare <run-id> --variant correct
faultline report <group-id>
```

Predict the outcome before each command: the unsafe replay should show matching
duplicate-debit failures; the comparison should PASS and list `variant` among its
differences. Then edit any Python file in `src/faultline` and try the replay again:
it should be refused because the adapter content hash changed.

## Tests

- [`tests/test_groups.py`](../../tests/test_groups.py): a controlled mixture of
  matching FAIL, other FAIL, PASS, INCONCLUSIVE, ERROR-with-violation, and interrupted
  trials produces exact counts and denominators; zero denominators; persistence.
- [`tests/test_replay.py`](../../tests/test_replay.py): exact plan reconstruction
  (including generated workloads), adapter/engine/image/platform drift, unsupported
  plan versions, missing environment identity, semantic drift in comparison, and CLI
  behavior with Docker replaced by a test double.
- [`tests/test_replay_integration.py`](../../tests/test_replay_integration.py): the
  real Docker path. An unsafe run fails, replays reproduce it from fresh baselines with
  identical plans and image IDs, and a comparison against the corrected variant passes.

## Limits

- Replay does not reproduce operating-system or database scheduling.
- Strict replay refuses after any engine source change. This is intentional, but it
  means old evidence cannot be strictly replayed after an upgrade; use comparison.
- Comparison trusts the current project's images. It reports image differences but
  cannot tell you *why* an image differs.
- Group records are local JSON files. They have no integrity hash of their own beyond
  pointing at hash-verified run bundles.
