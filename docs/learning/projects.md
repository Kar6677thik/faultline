# Projects: the laboratory's equipment list and spending limits

A **scenario** is an experiment recipe: make this purchase, hide this response, and permit this retry. A **project** describes the laboratory around it: which application image to run, which services are allowed, and how much time and computing capacity the experiment may use.

ELI5: the scenario says what to cook. The project says which kitchen to use and how much time and electricity you can spend.

Faultline now runs a strict project-manifest subset for the existing order profile. Start with [the executable example](../../examples/orders/faultline.project.yaml). This is not the complete five-service public v1 project contract: proxy services, external adapter packages, flexible routing, and additional domains remain unsupported.

## Run the example

Use the supported Linux/WSL workspace after the README install/build steps:

```bash
uv run faultline --project examples/orders/faultline.project.yaml --format json validate lost-response.yaml
uv run faultline --project examples/orders/faultline.project.yaml doctor
uv run faultline --project examples/orders/faultline.project.yaml run lost-response.yaml --variant correct
uv run faultline --project examples/orders/faultline.project.yaml run lost-response.yaml --variant unsafe
```

The unsafe run should detect the deliberate duplicate debit and exit 1. ERROR or INCONCLUSIVE is not successful defect detection.

When a project is selected, `lost-response.yaml` is relative to the directory containing that project file. Do not prepend `examples/orders/` again. The artifact output path still defaults to `.faultline` relative to the current working directory; use the global `--output` option to choose another location.

Without `--project`, validation, execution, and doctor check for `faultline.project.yaml` in the current directory only. They do not search parent directories. If no file exists, the original README commands continue using the implicit built-in environment. If a default file exists but is invalid, the command fails instead of silently ignoring it.

`report` and `clean` use recorded bundles. They do not read current project files, even if `--project` is present. You must be able to inspect and clean an old experiment after its YAML has changed or disappeared.

## What the current manifest accepts

| Field | Meaning and current supported contract |
| --- | --- |
| `schema_version` | Integer 1; this implementation accepts a restricted subset of that schema |
| `project_id` | A short stable name used in evidence and Docker ownership labels |
| `adapter` | Reserved built-in name `bundled_orders`, contract version 1, and the exact installed Faultline package version |
| `services` | Exactly `application`, `database`, `worker`, and `observer`, with the fixed role/port/readiness bindings below |
| `routes` | Exactly `orders_api`, application service, internal port 8000, base path `/` |
| `variants` | One or both of `unsafe` and `correct`, explicitly declared, each selecting an application image |
| `scenario_paths` | Optional list of up to 100 distinct project-relative files for discovery/documentation; does not execute them |
| `limits` | Optional local ceilings for run budgets and supported workload counts |

Although the selection field is called `entry_point` to follow the target shape, this increment uses a reserved built-in lookup. It does not import arbitrary installed entry points. `external_registration: false` remains visible in the adapter description. General variant names and the full specification's omitted-variants/default-variant behavior are not implemented; this profile requires explicit known variants.

If a project declares one variant, `run` may omit `--variant`. If it declares both, selection is required. No variant is inferred from previous runs or the scenario name. The variant selects fixture/application behavior, not verifier rules.

The current service bindings are:

| ID | Role | Internal ports | Readiness check |
| --- | --- | --- | --- |
| `application` | `application` | 8000 | `orders_health` |
| `database` | `postgres` | 5432 | `postgres_ready` |
| `worker` | `worker` | none | `worker_handshake` |
| `observer` | `observer` | none | `observer_snapshot` |

These names describe the existing implementation. The observer demonstrates readiness through its independently read baseline after fixture setup; the worker performs its persistent protocol handshake. The existing PostgreSQL and HTTP health checks retain their current helper timeouts and polling. Per-service `readiness.timeout_ms` and `poll_ms` overrides are rejected in this subset; stage budgets still bound the overall work.

Environment mappings and extra writable-path lists must be empty. Faultline continues to generate dependency credentials, set its internal connection/variant variables, and assign ephemeral writable areas itself. Arbitrary commands, mount settings, host networking, published ports, added capabilities, and privileged mode are unknown fields and fail validation.

## Locally available images, pinned for execution

The database keeps the bundled PostgreSQL digest. Worker and observer keep `faultline-runtime:0.1.0`; preflight resolves that local tag to its immutable image ID. The selected variant may choose a different locally available application image by tagged name, digest reference, or full `sha256:` image ID. Untagged references and URL syntax are rejected. Registry names with explicit port numbers are outside the current reference grammar.

The variant's application image overrides the application service's declared base image. Since this profile requires explicit variants, the selected variant is always authoritative. An unused base or unselected variant image is not resolved during `run`; project `doctor` checks every declared variant. All images actually selected must be Linux x86-64.

Preflight performs local image lookups only. It never pulls, builds, or runs an image while validating. Missing images fail before any experiment resources are created. Repeated trials reuse the already resolved IDs, so moving a tag during a command cannot quietly change a later trial's application.

A custom image is trusted application code and must satisfy this particular order fixture. It must listen on port 8000, provide `/health` and `/orders`, use the same PostgreSQL record contract and graceful-exit behavior, and run as UID/GID `10001:10001` with the restricted filesystem/capability settings. The engine supplies `FL_APP_PASSWORD`, `FL_VARIANT`, and the private database hostname `database`, port 5432, database `faultline`, role `fl_app`. Inspect [orders.py](../../src/faultline/orders.py) for the present connection/schema contract. An image that needs another schema, ports, permissions, or environment cannot be integrated by adding unsupported YAML fields.

Accepting a local image is not an independently demonstrated external application integration. The reference tests still use the existing image; the separately packaged public-adapter acceptance gate remains outstanding.

## Three kinds of budgets

**Container resources** describe the computing limits Docker enforces. Each service declares memory bytes, CPU milliseconds per second, and maximum process IDs. A memory value of `201326592` is 192 MiB. `cpu_millis: 500` permits half a CPU's scheduling capacity; `1000` permits one CPU. The PID limit bounds processes/threads counted by the container cgroup, not the number of API requests.

The supported ranges are 64–256 MiB, 100–1000 CPU millis, and 16–64 PIDs per service. Preflight compares aggregate declared memory and CPU with Docker's reported total host capacity. It does not measure currently free resources or promise successful startup under every low allocation. Memory pressure or insufficient runtime resources can still cause a harness error. Database tmpfs and helper writable areas keep their existing fixed bounds.

**Project limits** are ceilings: the maximum budget this laboratory permits. **Scenario limits** are the budget requested by this experiment. Omitted project ceilings use engine hard caps, rather than the smaller scenario defaults.

For example:

```yaml
limits:
  execution_ms: 20000
  total_ms: 90000
  cleanup_ms: 30000
  max_operations: 1
  max_attempts: 5
  max_faults: 1
```

If a scenario omits `execution_ms`, its normal 60000-ms default becomes 20000 ms. If the scenario explicitly requests 30000 ms, validation rejects it. Silently rewriting that explicit request would change what the author asked to test.

After merging, Faultline revalidates the complete scenario. A clipped execution budget must still cover all planned attempts and backoffs. Total time must exceed reserved cleanup. A smaller ceiling does not make an impossible experiment valid. Explicit nulls are rejected: omit a field to choose its default behavior.

This profile supports one logical operation, at most five potential attempts, and at most one fault. `max_faults: 0` can restrict a project to controls. These count ceilings do not enable concurrency or the larger release limits. Every effective run deadline is saved in `plan.json` and shown by JSON validation output.

## File paths and strict YAML

[models.py](../../src/faultline/models.py) now provides the shared bounded YAML loader. It reads at most one MiB from a regular file, rejects anchors/aliases, duplicate fields, excessive nesting, nonfinite numbers, and non-JSON types such as YAML dates. It rejects FIFOs without blocking on a writer. YAML syntax errors do not echo rejected source lines into CLI diagnostics.

[projects.py](../../src/faultline/projects.py) resolves scenario paths relative to the selected project directory. Absolute paths, backslashes, drive/URL prefixes, empty components, `.`/`..` components, symlink descendants, and non-files are rejected. Declared discovery files must exist, but their scenario contents are not loaded or executed unless selected. The selected scenario need not appear in `scenario_paths`.

The selected project parent is resolved once. These checks protect ordinary local configuration handling; they are not a sandbox against a hostile process concurrently replacing directory components. Execution and the nonblocking/no-follow file checks target Linux/WSL as documented.

## What gets saved, and why cleanup still works

New project-backed runs include `project.json`, containing the validated configuration with explicit service settings and optional defaults. A canonical configuration hash and project ID appear in the manifest and JSON report. The workload plan records effective scenario limits, while selected image references and resolved IDs live in platform metadata. Project identity and image selection do not enter the business-plan hash; changed effective scenario limits do.

Docker resources receive the project ID alongside the existing managed/run/nonce labels. The manifest saves the project ID before resource creation. Recovery checks recorded IDs and every ownership label using that saved value; it never reloads YAML to decide what to delete. Legacy manifests without a project ID still use their original `orders-m1` label. The output-root lock and resource-inventory restrictions remain in force.

In [engine.py](../../src/faultline/engine.py), direct Python callers also revalidate project settings and ceilings. Injected preflight information must identify the same project configuration and selected variant, including a resolved application image, before a bundle can run. This prevents accidentally pairing one configuration with another's preflight results; trusted Python callers are not a sandbox boundary.

## Follow the code and test the reasoning

- [projects.py](../../src/faultline/projects.py): schemas, supported topology, variant selection, budget merging, and path resolution.
- [cli.py](../../src/faultline/cli.py): explicit/default project selection and CLI integration.
- [environment.py](../../src/faultline/environment.py): local image resolution, host-budget checks, and Docker limits.
- [artifacts.py](../../src/faultline/artifacts.py) and [recovery.py](../../src/faultline/recovery.py): saved configuration and ownership independent of current YAML.
- [test_projects.py](../../tests/test_projects.py): unsafe settings, paths, ceilings, local-only preflight, and ownership contracts.
- [test_integration.py](../../tests/test_integration.py): inspect live container limits and images, check real business verdicts, select a sole project variant through the CLI, and recover project-owned leftovers.

Try lowering the project execution ceiling in a copied example. First omit the scenario's execution limit and inspect the effective value with `faultline --format json validate SCENARIO`. Then explicitly request a value above the ceiling. Explain why one succeeds and the other is rejected before running anything.
