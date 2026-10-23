# Why Faultline needs an adapter

Imagine Faultline as a laboratory supervisor. The supervisor knows how to book the room, start a stopwatch, interrupt an experiment, record observations, and put the equipment away. A lab assistant knows the particular experiment: which starting data to create and which business rules to check.

The **engine** is the supervisor. The **adapter** is that assistant. For our shop experiment, the assistant understands orders, wallet debits, stock, and confirmation records. A future bank-transfer assistant will understand accounts and equal debit/credit pairs instead.

This guide covers the **checked internal adapter boundary** for the existing order experiment. It does not yet let you install another adapter or introduce another business domain. The later [project configuration increment](projects.md) permits selecting a local application image that conforms to the same order contract. The [public release contract](../../specs/04-adapters.md) remains the destination; the current bridge is deliberately narrower.

## Why this change matters now

Previously, the engine imported the order verifier directly, and container provisioning also initialized the order database. That made infrastructure work and domain work harder to separate.

Now [engine.py](../../src/faultline/engine.py) coordinates hooks in [adapters.py](../../src/faultline/adapters.py). A **hook** is simply a named function called at a particular point in the experiment. Docker creation, ownership checks, stage timers, fault scheduling, and teardown stay with the engine and [environment.py](../../src/faultline/environment.py).

There is a second reason to introduce a boundary: the assistant can make a mistake. Suppose seven business rules are required, but the verifier returns only six passing results. Six green checks do not prove that the seventh rule passed. Faultline now rejects that result as a harness ERROR.

## What each hook does

| Hook | Plain-language question | Current behavior |
| --- | --- | --- |
| `describe` | What can this assistant handle? | Describes one order profile, one fixture, the two variants, seven versioned invariants, concurrency one, and response suppression |
| `validate` | Is this recipe supported? | Checks capabilities and validates the current scenario model before Docker preflight |
| `prepare` | Is the starting arrangement correct? | Requires a successful setup acknowledgment, reads the baseline through the observer, and independently checks the fixture |
| `readiness` | Can the application accept work? | Performs bounded HTTP health checks after the application and worker start |
| `quiesce` | Did the application exit gracefully? | Returns a strict boolean; the final observer separately checks that database application sessions have ended |
| `snapshot` | What is actually in the notebook? | Reads authoritative PostgreSQL state and checks its envelope, identities, types, counts, and content hash |
| `verify` | Did the rules survive? | Evaluates the plan, baseline, final state, and attempts without receiving the environment or variant |

Readiness is not fixture validation. An HTTP health endpoint can respond successfully even when the starting balance is wrong. Likewise, graceful process exit alone is not our complete quiescence proof: final observation must also show zero application database sessions. The engine combines both facts before allowing the final state to be treated as settled.

Provisioning, preparation, and readiness share the provisioning budget. Quiescence uses the drain budget; final snapshots use observation; verification and validation of its returned findings use verification. Cleanup still runs when a hook fails. The built-in description and input validation run before the timed experiment, like existing scenario parsing; arbitrary installed hooks are not supported by this arrangement.

## Checking the assistant's work

`OrdersAdapter` supplies domain behavior. `CheckedAdapter` checks the shape and completeness of its results. Neither replaces the independent business reasoning in [verifier.py](../../src/faultline/verifier.py).

For a snapshot, the boundary requires the supported schema, the requested baseline/final identity, the expected profile and fixture, PostgreSQL authority, read-only repeatable-read isolation metadata, explicit booleans for completeness and quiescence, collection timestamps, a transaction identifier, and an application-session count. Counts must match the preserved rows, and the content hash must match the records. Session evidence cannot contradict the observer's quiescence flag. Baselines must be complete and quiescent; a final observation may be incomplete, leading to UNKNOWN findings.

The domain verifier additionally checks exact table/row shapes and starting fixture contents. A hash alone cannot establish that the rows are correct: it only detects a mismatch against the recorded content.

For verification results, the boundary requires:

- Exactly one result for each requested invariant, with no omissions, duplicates, or unexpected rules.
- The declared invariant version and the requested logical purchase identity.
- PASS, FAIL, or UNKNOWN as the individual result status.
- Concrete failures and one signature per failure for FAIL; no failure list hidden inside PASS or UNKNOWN.
- Evidence references for results and failures, and an explanation for UNKNOWN.
- JSON-compatible values and a one-MiB limit per returned snapshot or complete findings list.

Returned snapshots and findings are copied through canonical JSON. A later change to a dictionary retained by the hook cannot silently change the engine's saved value. The size ceiling is a result-validation limit, not a memory sandbox: serialization and hook execution occur in this trusted Python process.

A hook that raises an ordinary exception produces `adapter_hook_error`. A malformed return produces `adapter_contract_error`. Both are ERROR, with the hook name recorded. Arbitrary exception text and return payloads are not copied into diagnostics because they may contain credentials. The original exception remains chained for local debugging. Cancellation and stage-deadline signals bypass this wrapping so the engine retains their existing semantics.

These checks prevent malformed results from being accepted. They cannot prove that an otherwise well-formed verifier tells the truth. Independent hand-audited fixtures and real known-defect experiments remain essential. Adapters are trusted code, not sandboxed plugins.

## Identifying the assistant that graded a run

The new `adapter.json` artifact records the capability description and source identity. The manifest and JSON report contain a compact reference to it, and the history includes an `adapter_selected` event before provisioning resources.

An identity contains the package name/version, individual Python-source SHA-256 hashes, and a hash of that sorted source inventory. The bundled adapter shares a package with the engine and verifier, so the inventory covers the package's Python files rather than guessing which imports might affect correctness. At most 128 Python files, one MiB per file, and four MiB total are accepted.

ELI5: write down which edition of the instruction book the assistant used. If you change a page, the book's fingerprint changes.

The hash uses exact installed source bytes, including line endings. A CLI-only source change can change this broad fingerprint; changing README text does not. Package versions alone are insufficient because an editable checkout can change without a version bump.

This is source provenance, not a digital signature, a hash of all third-party dependencies, or proof that running memory and container code match the checkout. Runtime image IDs remain separately recorded. Rebuilding the runtime after source changes is still required. In-memory monkeypatches used by tests are not represented by the disk-source hash. Strict historical replay and compatibility enforcement remain future work.

Offline `report` reads saved evidence without calling adapter hooks. Existing completed bundles without `adapter.json` remain readable. New bundles inventory and hash the artifact, so modifying it is detected by the existing corruption check.

## Try it

From the supported Linux/WSL workspace:

```bash
uv run faultline --format json validate examples/orders/lost-response.yaml
```

Look under `summary.adapter`. It explicitly says `external_registration: false`. No Docker access is needed for this command.

After rebuilding the runtime and running the README demo, open the run's `adapter.json` and `report.json`. Compare their `content_hash` values, then follow the inventory to `verifier.py`.

For a learning exercise, inspect `test_invalid_verifier_output_is_never_a_business_verdict` in [test_adapters.py](../../tests/test_adapters.py). Predict what happens when a verifier returns six PASS results. Then read `test_malformed_adapter_finding_cannot_pass_real_purchase` in [test_integration.py](../../tests/test_integration.py): it performs a real committed purchase, deliberately drops one result, and checks ERROR plus complete cleanup. A sound application and a broken judge are different outcomes.

## What remains before external adapters

The scenario parser still has order-specific types; the HTTP scheduler still sends the one bundled order request; the environment still uses the fixed dependency images and topology. The [project increment](projects.md) adds a runnable restricted manifest, local application image selection, and resource/budget settings. It does not load external adapter packages. The internal `boundary_version: 1` is not a claim of compliance with the full public adapter API version 1. The current boolean quiescence result is narrower than the planned structured ready/pending/unknown protocol.

External registration still needs a generalized project manifest, pinned installed-package identity, capability negotiation across routes and services, explicit lifecycle contracts, and a separately packaged integration test. The internal boundary and restricted projects create tested places to introduce that work without changing the current experiment's semantics.
