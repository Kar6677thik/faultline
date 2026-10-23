# One purchase, from command to evidence

## 1. Describe the experiment

Open [lost-response.yaml](../../examples/orders/lost-response.yaml). YAML is a readable way to write structured settings. This file is the experiment recipe, not Python code.

- `schema_version` identifies the settings format.
- `id` names the scenario; `adapter_profile` selects the order rules and `fixture` selects the starting data. These remain fixed order-profile choices even when a project selects an installed adapter.
- `seed` is saved for reproducible input descriptions. This milestone has an explicit operation and no random generator, so changing the seed does not generate a different schedule.
- `concurrency: 1` means one attempt at a time. General concurrent workloads are not supported yet.
- `operations` describes one logical purchase. Its `id` is the scenario's local label; `logical_id` equals the business `purchase_id`.
- `request` describes the HTTP method, route, idempotency key, and body. The body requests a quantity, not a trusted client-supplied price.
- `retry` permits a second attempt on timeout or connection failure. `backoff_ms: [100]` means wait 100 milliseconds before that second attempt.
- `timeout_ms: 1000` gives an attempt one second. One second is 1,000 milliseconds.
- `faults` says to hide the first complete 201 response for this operation. `required: true` means we cannot claim the intended experiment passed unless that fault actually occurred.
- `invariants` lists all seven rules the verifier must check.

[models.py](../../src/faultline/models.py) rejects unknown fields, mismatched purchase identities, impossible retry numbers, excessive sizes, and unsupported capabilities. It also rejects duplicate YAML fields. Two conflicting instructions must cause an error rather than silently choosing one. The parser deliberately treats `on` as a key, not the boolean `true`, avoiding a YAML 1.1 surprise.

`faultline validate` performs these checks without Docker. Catching an invalid recipe before building the laboratory saves time and prevents ambiguous experiments.

[adapter_loader.py](../../src/faultline/adapter_loader.py) selects the built-in or explicitly installed adapter under a separate ten-second preflight allowance. [adapters.py](../../src/faultline/adapters.py) checks whether it supports the recipe and exposes its capabilities in validation output. The engine calls this checked boundary for domain work. Think of the engine as the lab supervisor and the adapter as the assistant who understands the shop. Read the [adapter guide](adapters.md) for hooks and result checks, and the [installed-adapter guide](installed-adapters.md) for package selection, provenance, and its trust boundary. Loading a package does not introduce another business domain.

Optionally select the [example project](../../examples/orders/faultline.project.yaml) with global `--project`. [projects.py](../../src/faultline/projects.py) validates the lab's service settings, constrains scenario files to its directory, and merges budget ceilings before adapter validation. The [project guide](projects.md) explains why an omitted default can be clipped while an explicit over-ceiling request is rejected. With a project, scenario paths are project-relative; artifact output remains relative to the current directory.

## 2. Enter through the CLI and acquire the lock

[cli.py](../../src/faultline/cli.py) translates terminal arguments into engine calls. Typer handles command parsing and help. `python -m faultline` enters through [__main__.py](../../src/faultline/__main__.py); the installed `faultline` command reaches the same CLI.

[__init__.py](../../src/faultline/__init__.py) identifies the package version, which the artifact writer includes in run metadata. The version describes the tool, while each run UUID identifies one particular experiment.

The output root defaults to `.faultline`. Each run gets a new UUID and a directory under `runs/`. Global options such as `--output` and `--format json` go before the command.

[locking.py](../../src/faultline/locking.py) holds an operating-system lock on `execution.lock` for each call to `run_one`. Another run or cleanup operation using the same output root must wait for a later invocation; it is refused immediately rather than queued. The lock prevents cleanup from deleting a live experiment's resources. The file can stay on disk after the process exits; the operating system's lock is the part that establishes liveness.

[deadlines.py](../../src/faultline/deadlines.py) gives each stage a timer and reserves part of the total run budget for cleanup/reporting. Think of keeping packing-up time at the end of a laboratory booking. The supported coordinator uses Linux main-thread alarms so a blocked stage can be interrupted. Read the [deadline guide](deadlines.md) for settings, the arithmetic, and the limits of that approach.

## 3. Build a disposable laboratory

[environment.py](../../src/faultline/environment.py) talks to the local Docker daemon. A daemon is the background service that creates and manages containers. Preflight checks require Linux x86-64, a local daemon, and the already-built images. Experiments do not pull images automatically.

There are four containers:

- **Application:** runs FastAPI and the order endpoint.
- **Database:** runs PostgreSQL and holds the wallet, stock, and purchase records.
- **Worker:** sends real HTTP requests from inside the private client network.
- **Observer:** initializes the fixture and later reads authoritative state. Setup uses write-capable credentials; observation uses a separate read-only database role. The helper container is trusted and holds the required credentials; it is not a sandbox for hostile code.

There are two internal networks. The client network connects worker and application. The storage network connects application, database, and observer. There are no published application ports. This is currently direct database access; a future proxy will require a different topology.

An image is a packaged program. A container is a running or stopped instance of that image. The runtime image is built by the [Dockerfile](../../Dockerfile); the host and runtime package versions must match the code being tested. Image digests identify exact image content.

A project can select a local application image that conforms to the same order fixture, and lower per-container CPU/memory/PID allocations within supported bounds. Preflight checks host capacity and image architecture and pins selected image IDs. Dependency/helper images and the private network topology remain constrained. No image is pulled during preflight or execution.

The environment records every returned Docker resource ID and labels the resource with Faultline ownership information. Cleanup needs both the record and matching labels. A name such as `fl-example` alone is not authorization to delete anything.

## 4. Create known starting data

[orders.py](../../src/faultline/orders.py) creates a fresh schema. A fixture is simply a known starting arrangement:

- Account `buyer-a` has 1,000,000 paise.
- Product `sku-a` costs 200,000 paise and has ten units in stock.
- There are no purchases or related effects yet.

The database contains seven tables:

| Table | Plain-language purpose |
| --- | --- |
| `accounts` | How much money each buyer currently has |
| `products` | The authoritative price and remaining stock |
| `orders` | The confirmed purchase and its immutable business result |
| `ledger` | Individual money movements, including the row ID and purchase identity |
| `movements` | Individual stock changes linked to the purchase |
| `outbox` | A saved intention to send an order-confirmed event |
| `idempotency` | The retry key, request fingerprint, and result it belongs to |

The adapter's `prepare` hook requires the setup helper's acknowledgment, then reads a baseline snapshot before sending work. [verifier.py](../../src/faultline/verifier.py) independently checks that the starting state really matches the fixture. The environment creates resources but no longer initializes the domain schema itself. A broken setup must not be graded as an application defect.

## 5. Perform a real transaction

The worker sends `POST /orders` with a purchase ID and an `Idempotency-Key`. The purchase ID means “this business purchase.” The retry key means “these requests are retries of the same request.” The contract protects both identities, so changing a retry key must not authorize charging twice for one purchase.

In the correct backend, the database transaction locks the buyer's account row before checking existing results. This is like taking the only key to that buyer's notebook: another transaction needs to wait before changing the same account. The implementation also locks product stock before a new purchase, checks funds and stock, and uses uniqueness constraints as database-enforced identity rules.

For an existing matching purchase it returns the stored result. For a conflicting payload it rejects the request. For a new purchase it creates the order, ledger entry, stock movement, outbox intent, and retry record while updating balance and stock in one transaction. These changes commit together. An exception causes rollback rather than leaving half the purchase behind.

The unsafe backend still performs real transactions and ordinary valid purchases. Its deliberate defect is bypassing purchase/retry uniqueness and duplicate protection. Consequently, a retry can create a second complete purchase. A transaction by itself cannot tell two separate transactions that they represent the same logical purchase.

## 6. Hide a successful response and retry

[worker.py](../../src/faultline/worker.py) keeps a helper process running. It sends correlated JSON messages through Docker's exec stream. Correlation means matching a reply to the request that caused it. Docker adds framing bytes around output; the worker session decodes them and enforces size/deadline limits.

[remote.py](../../src/faultline/remote.py) runs inside the worker. HTTPX sends the request and reads its response under a total async deadline. Redirects and environment proxy settings are disabled. Only the built-in internal routes are accepted.

[engine.py](../../src/faultline/engine.py) owns retry decisions and fault application. When it receives the complete matching successful response before the attempt deadline, it records that response and the fault, withholds the response from the retry policy, and reports a timeout at the deadline. The policy then issues the second attempt.

The application has already committed. The client acts as if it did not receive the success. This reproduces the important ambiguity of a lost reply, but it is **controlled response suppression**, not actual TCP packet dropping.

The two outcomes are now observable:

- Unsafe: two orders, two debits, wallet reduced by 400,000 paise.
- Correct: one order and debit, the same purchase result returned again, wallet reduced by 200,000 paise.

## 7. Stop writes before inspecting final state

A timeout cannot prove that the server stopped working. Reading immediately could miss a debit that finishes a moment later.

For this tiny synchronous application, Faultline asks the application to exit gracefully and then checks that its PostgreSQL sessions have ended. Only after establishing that no supported writer remains can the final snapshot represent completed work. This state is called **quiescence**.

The observer reads all relevant tables in one read-only `REPEATABLE READ` transaction. This gives the queries a consistent database view. It retains individual rows, including duplicates and orphan effects; grouping records by purchase ID would hide the exact defect we want to detect.

Forced shutdown, a drain deadline, or uncertain quiescence prevents a full PASS. Even if the drain function returns success at the deadline boundary, expiry removes certainty. A future application with background workers will need a stronger, explicit quiescence contract; the current method does not cover it.

## 8. Check seven different questions

The verifier takes the plan, attempt results, and snapshots. It does not call the application or branch on `unsafe` versus `correct`. It judges recorded facts.

The adapter boundary then checks that all seven requested invariant results are present exactly once, have the right versions and purchase identity, and contain consistent statuses and evidence. A verifier that forgets a rule is a harness ERROR even if its remaining results say PASS. The engine no longer imports the order verifier directly.

| Invariant | Simple question |
| --- | --- |
| `at_most_one_debit` | Did one logical purchase debit the wallet more than once? |
| `purchase_atomicity` | Are the order, debit, and stock effect present together and correctly linked? |
| `wallet_conservation` | Does the ending balance agree with the opening balance and recorded money movements? |
| `inventory_conservation` | Does remaining stock agree with the opening stock and recorded stock movements? |
| `confirmation_intent` | Does each committed purchase have its corresponding durable confirmation intent? |
| `idempotency_contract` | Do repeated attempts, stored identities, and returned results agree? |
| `completed_purchase` | Did the intended purchase actually complete once? |

The last check matters: a server that refuses every request could satisfy “never double charge” while being useless. Safety checks need a completion requirement for this scenario.

Individual checks use PASS, FAIL, or UNKNOWN. The overall report gives harness errors precedence over business failures, while retaining any detected violations:

1. **ERROR:** the testing machinery or cleanup failed.
2. **FAIL:** trustworthy evidence demonstrates a violated rule.
3. **INCONCLUSIVE:** required evidence, workload completion, or fault coverage is missing.
4. **PASS:** the required checks and experiment coverage succeeded.

A definite business violation can remain FAIL even if the requested fault was not applied. That does not make it a successful reproduction of the requested experiment; acceptance tests check fault coverage separately.

## 9. Save evidence and remove the laboratory

[artifacts.py](../../src/faultline/artifacts.py) writes the plan, manifest, event history, snapshots, and reports. JSON stores structured objects; NDJSON stores one JSON event per line. Sequence numbers expose missing or reordered history entries. Hashes detect file changes relative to the recorded inventory; they are not digital signatures and do not prove who created an artifact.

New runs also save `adapter.json`: capabilities, package version, and the source-file inventory fingerprint. Installed adapters additionally record their distribution file inventory and factory address, combined with Faultline's source hash. Project-backed runs save `project.json` and its configuration hash. The manifest and report reference these identities. This helps identify the judge and laboratory used; it does not implement historical replay or prove host/container source equivalence. Cleanup uses the project label saved before resource creation and does not reload current YAML or resolve adapters.

Writes use a temporary file and replacement for complete JSON documents. History events are appended and flushed to disk. These choices reduce partial-write ambiguity; they do not promise recovery from every disk failure or power loss.

Cleanup removes recorded containers before their networks and verifies ownership immediately before deletion. Its budget includes a small reporting reserve. If deletion runs out of time, unresolved recorded IDs and completed findings are retained in an ERROR report when writing can finish. Reports remain readable without Docker. [recovery.py](../../src/faultline/recovery.py) handles later cleanup after a hard kill or failed teardown; it never changes the original experiment verdict.

## 10. Connect claims to tests

- [test_scenarios.py](../../tests/test_scenarios.py) checks recipe rejection and supported contracts.
- [test_runner.py](../../tests/test_runner.py) checks retries, response suppression, and timing decisions with controlled inputs.
- [test_worker.py](../../tests/test_worker.py) checks message framing, truncated streams, frame-size limits, and socket cleanup.
- [test_verifier.py](../../tests/test_verifier.py) uses hand-built records to check the judge independently of the application's transaction code.
- [test_adapters.py](../../tests/test_adapters.py) checks the judge's result contract, malformed snapshots, capability refusal before Docker, hook errors, and source provenance.
- [test_adapter_loader.py](../../tests/test_adapter_loader.py) checks real installed metadata/imports, ambiguous registrations, wrong versions, missing/unsafe files, import-path mismatch, bounded hooks, and cancellation. The Docker suite separately builds and installs an actual wheel.
- [test_projects.py](../../tests/test_projects.py) checks strict manifests, confined paths, ceiling/default merging, local image selection, host budgets, and recorded project ownership.
- [test_artifacts.py](../../tests/test_artifacts.py) checks evidence integrity and incomplete/corrupt bundles.
- [test_environment.py](../../tests/test_environment.py) checks ownership and failure-path cleanup.
- [test_cli.py](../../tests/test_cli.py) checks terminal contracts and secret-safe validation output.
- [test_recovery.py](../../tests/test_recovery.py) checks unsafe manifests, file paths, lock exclusion, and cleanup receipts.
- [test_deadlines.py](../../tests/test_deadlines.py) checks stage/total budgets, real alarms, partial evidence, uncertain drain, and bounded teardown/finalization.
- [test_integration.py](../../tests/test_integration.py) runs actual Docker/PostgreSQL experiments, including real process signals.

A unit test answers a narrow question quickly. An integration test checks whether the real pieces work together. Both matter: a mocked database cannot prove our actual SQL transaction and permissions behave as expected.
