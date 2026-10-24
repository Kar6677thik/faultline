> M2 update: installation/provenance rules here still apply. The inherited order wrapper remains a packaging example; [the counter adapter](external-adapters.md) now demonstrates an independent application, schema, and verifier.

# Installing a different laboratory assistant

The engine is the laboratory supervisor; an adapter is the assistant who understands the business rules. Previously the assistant had to come inside Faultline's own box. Now a project can choose an assistant delivered in a separate Python package.

This increment proves **package installation and selection for the existing order experiment**. It does not yet make Faultline a general testing tool for arbitrary business domains. The recipe, order request, four containers, database schema, seven required invariants, and graceful-shutdown observation remain the current restricted contract. The [public v1 adapter specification](../../specs/04-adapters.md) describes the larger destination.

## Three names that mean different things

The [packaged example](../../examples/installed-orders-adapter/pyproject.toml) uses:

| Name | Meaning | Example |
| --- | --- | --- |
| Distribution | The installable Python package, like the label on a delivery box | `faultline-example-orders` |
| Entry point | A registered name the project selects | `example_orders` |
| Factory | A Python callable that creates a fresh adapter with no arguments | `faultline_example_orders:ExampleOrdersAdapter` |

An **entry point** is a small address card installed with a package. It says, "If someone asks for this name, call this function or class." Installing a package does not automatically select it. Project configuration selects one name from the `faultline.adapters` entry-point group:

```yaml
adapter:
  entry_point: example_orders
  contract_version: 1
  package_version: 0.1.0
```

The exact package version must match the installed distribution. Faultline does not interpret `>=0.1`, download packages, run `pip`, evaluate a module path from YAML, or fetch an adapter from a URL. Missing names, duplicate registrations (including two installed copies of the same distribution), and version mismatches fail before Docker preflight.

`bundled_orders` remains a reserved built-in name. An installed package cannot replace that name. All other names must be simple identifiers matching the project schema. This selection rule is deliberately smaller than the eventual generalized public API.

## Try the packaged example

Use Linux/WSL in the repository, after the README setup and runtime build. The first wheel build may download build tooling. The root `uv.lock` still supplies the runtime dependencies; `--no-deps` below prevents installing a second copy of Faultline from an index.

```bash
uv build examples/installed-orders-adapter --wheel --out-dir .faultline/adapter-wheels
uv pip install --python .venv/bin/python --no-deps \
  .faultline/adapter-wheels/faultline_example_orders-0.1.0-py3-none-any.whl

mkdir -p .faultline/installed-example
cp examples/orders/*.yaml .faultline/installed-example/
sed -i 's/entry_point: bundled_orders/entry_point: example_orders/' \
  .faultline/installed-example/faultline.project.yaml

.venv/bin/faultline --project .faultline/installed-example/faultline.project.yaml \
  --format json validate lost-response.yaml
.venv/bin/faultline --project .faultline/installed-example/faultline.project.yaml \
  run lost-response.yaml --variant unsafe
.venv/bin/faultline --project .faultline/installed-example/faultline.project.yaml \
  run lost-response.yaml --variant correct
```

Expected outcomes are FAIL with two debits for unsafe and PASS with one debit for correct. FAIL deliberately exits 1. Handle it explicitly if running a shell script with `set -e`.

Use `.venv/bin/faultline` after the installation to run that exact environment. `uv run --no-sync faultline ...` is another option. A later exact synchronization (`uv sync` or `uv run --exact`) can remove the separately installed package because it is absent from the root lock; reinstall the wheel if needed. This example is optional and is not added to Faultline's runtime dependencies.

To try the deliberately broken grader, change the same project's entry point to `incomplete_orders` and run the correct application. It returns only six of seven required findings. Expected outcome: **ERROR**, with `adapter_contract_error` at `verify`, even though the database contains one correct purchase. Restore `example_orders` afterward. To uninstall the example:

```bash
uv pip uninstall --python .venv/bin/python faultline-example-orders
```

Saved reports and scoped cleanup continue working without that package or the project file. The integration tests exercise that absence using a separate temporary installation, leaving your normal environment unchanged.

## The contract a package must satisfy

The factory must return an object with `describe`, `validate`, `prepare`, `readiness`, `quiesce`, `snapshot`, and `verify` callable methods. Their arguments and return checks are explained in the [adapter guide](adapters.md) and implemented by [CheckedAdapter](../../src/faultline/adapters.py). An installed description must use its selected entry-point name as `adapter_id` and set `external_registration: true`.

All declared project variants must be supported. Scenario capability checks still reject unsupported operations, faults, fixtures, concurrency, and invariant sets. Successful loading does not broaden the parser or service topology. `contract_version: 1` and `boundary_version: 1` identify this bridge, not completion of the whole public API v1.

`prepare` must initialize a complete fixture and independently validate its baseline. `snapshot` must retain authoritative rows including duplicates. `verify` receives the scenario, snapshots, attempt history, and workload-complete flag; it does not receive a variant or environment. It must be pure: no network calls, business mutations, or decisions based on the application variant. Structural output checks cannot establish that its reasoning is sound. Known-good and known-bad calibration remains necessary.

An adapter is **trusted code running inside the coordinator**, like installing a normal Python library. It is not sandboxed. Hooks can technically reach Python and OS capabilities; purity and ownership restrictions are author obligations, not a security boundary enforced against hostile plugins. `describe`/`validate` must avoid side effects, spawning work, and printing into CLI JSON. The engine owns resource creation and teardown; an adapter must use the supplied owned environment and never target arbitrary endpoints.

The example subclasses the existing `OrdersAdapter` and changes its description. Its defective companion intentionally drops a finding. This proves package discovery, real dispatch through the checked boundary, authoritative observation, and malformed-grader handling. It is **not an independently implemented verifier, a second correct application, or the full external-domain onboarding acceptance gate**.

## Why the tool writes down file fingerprints

A version label is like a book edition number. Someone could edit a page without changing the cover. A **content hash** is a compact fingerprint that changes when the measured pages change.

For an installed adapter, `adapter.json` contains its distribution name/version, selected entry point and factory, hashes of installed files, and the existing Faultline Python-source inventory. The combined hash covers the file inventory, factory address, and engine source hash. Including engine code matters because this example reuses Faultline's verifier: hashing only its small wrapper would miss changes to the actual judge.

The loader reads the installed wheel's `RECORD` file, which lists package files, and hashes their actual bytes. It does not trust the hash strings written inside `RECORD`. It parses the list directly because Python 3.13's convenience inventory omits missing files. A missing source must cause refusal, not a smaller apparently valid fingerprint.

Current bounds and restrictions are explicit:

- At most 1,024 installed distributions and 256 entry points per distribution during discovery.
- A `RECORD` of at most 64 KiB, at most 256 listed files, at most one MiB per measured file, and four MiB total measured package bytes.
- Relative paths inside the installation root; no symlink descendants, nonregular files, missing measured files, duplicate paths, or traversal components.
- Non-editable pure Python installation. `.pth`, `.so`, and `.pyd` files are refused. Packages that install console scripts outside this root are outside this subset.
- Generated `.pyc`/`.pyo` bytecode is excluded; the registered module must be listed as Python source. After import, its resolved source path must match the inventoried source.
- Faultline's own existing source inventory has separate limits: 128 Python files, one MiB each, four MiB total.

These are supported-package constraints and evidence checks, not tamper-proof attestation. Discovery/metadata parsing and serialization can allocate memory before size checks. Imported module caches, dependencies outside the selected package, unlisted files, native execution, malicious metadata, and concurrent file edits are not fully measured or isolated. The post-import path check cannot prevent import-time effects. Installer metadata and line endings can change fingerprints even when behavior is equivalent. Use a fresh CLI process after changing installed code. Keep runtime image IDs alongside package evidence; the hash does not prove host/container equivalence or implement strict replay.

## Deadlines and failure meanings

One fixed ten-second alarm covers discovery, provenance, import, factory construction, `describe`, and `validate` per resolution. It uses the same Linux main-thread timer mechanism as the [run deadlines](deadlines.md). It runs before Docker or output-root locking, separately from scenario budgets. Both bundled and installed selection follow this timing rule. CLI `run` checks once before Docker preflight and again inside each `run_one` call, so direct Python callers receive the same checks and each repetition receives a fresh adapter object. Imported Python modules may remain cached within the process.

`doctor` checks selection, identity, description, and project variant support before checking Docker; it has no scenario to validate. `validate` checks a scenario without Docker. Neither command provisions a fixture or proves the verifier's semantic correctness.

| Condition | Result |
| --- | --- |
| Missing/ambiguous entry point, version mismatch, unsupported capabilities | Invalid input/preflight, exit 2 |
| Unverifiable package files, factory/hook exception, malformed hook result | Harness ERROR, exit 4 |
| Adapter preflight exceeds its allowance | ERROR with `adapter_preflight_timeout`, exit 4 |
| Business invariant violated with sufficient evidence | FAIL, exit 1 |
| Evidence cannot establish the required result | INCONCLUSIVE, exit 3 |

Preflight errors have no run bundle because no experiment has begun. Runtime hook failures retain the existing stage deadlines, ERROR handling, and teardown. Hook diagnostics omit arbitrary exception text and payloads. Keyboard interruption is not converted into a plugin error. The alarm is not a hard real-time or hostile-code guarantee; trusted hooks must not disable it or leave background work behind. `report` and `clean` do not resolve adapters at all.

## Follow the code and tests

| Responsibility | Source and evidence |
| --- | --- |
| Strict selection fields without importing code during YAML parsing | [projects.py](../../src/faultline/projects.py) |
| Discovery, provenance, bounded loading, capability validation | [adapter_loader.py](../../src/faultline/adapter_loader.py), [test_adapter_loader.py](../../tests/test_adapter_loader.py) |
| Complete checked hook contracts | [adapters.py](../../src/faultline/adapters.py), [test_adapters.py](../../tests/test_adapters.py) |
| Save measured identity and run lifecycle hooks | [engine.py](../../src/faultline/engine.py) |
| Package address cards and sample implementations | [pyproject.toml](../../examples/installed-orders-adapter/pyproject.toml), [example source](../../examples/installed-orders-adapter/src/faultline_example_orders/__init__.py) |
| Build/install a wheel, observe real debits, reject missing findings, read/recover without package | `test_installed_wheel_runs_and_offline_tools_survive_its_absence` in [test_integration.py](../../tests/test_integration.py) |

The wheel tests run under the existing `FAULTLINE_INTEGRATION=1 uv run pytest -q tests/test_integration.py` command. They use `uv` to build the package, install it without dependencies into a temporary directory, and start a fresh CLI process with only that directory added to its import path. No test edits the engine to select the adapter. See [measured results](../installed-adapter-acceptance-results.md) for actual outcomes, distinct from these expected results.
