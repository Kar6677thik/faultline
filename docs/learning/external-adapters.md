# Bring another application to Faultline

An adapter is a translator and an independent examiner. It explains which requests
make sense for an application, prepares an empty test database, and tells Faultline
which database facts establish correctness. It is trusted installed Python code,
not code downloaded from a report or a sandbox for hostile plugins.

The [counter example](../../examples/counter-adapter/) is a separate Python package
and application image. Its database contains `counter` and `increments`, with no
order, wallet, inventory, or outbox tables. Its adapter does not inherit the order
adapter or import the order transaction. One logical increment of seven should
leave one increment row and total seven. Suppress its response and retry against
the unsafe variant, and the real application increments twice. The verifier reads
the raw rows and total independently.

## Run the external example

From the repository in Linux/WSL, build the normal runtime first, then:

```bash
uv build --wheel examples/counter-adapter --out-dir .faultline/counter-wheels
uv pip install --no-deps .faultline/counter-wheels/faultline_counter_example-0.1.0-py3-none-any.whl
docker build -t faultline-counter:0.1.0 examples/counter-adapter
uv run faultline --project examples/counter-adapter/faultline.project.yaml validate lost-response.yaml
uv run faultline --project examples/counter-adapter/faultline.project.yaml run lost-response.yaml --variant correct
uv run faultline --project examples/counter-adapter/faultline.project.yaml run lost-response.yaml --variant unsafe
uv run faultline --project examples/counter-adapter/faultline.project.yaml run generated.yaml --variant correct
```

The unsafe command intentionally exits 1 when the invariant detects duplicate effects.
An ERROR is not successful defect detection. First wheel construction can fetch
build tooling. Tests install the wheel into a temporary site directory and use
`PYTHONPATH`, keeping the main environment unchanged. Rebuild the counter image
after changing its package or its underlying Faultline runtime.

## Exact public hooks

Register a callable factory in the package's `faultline.adapters` entry-point group.
The project selects its entry-point name, contract version 1, and exact distribution
version. The example is `counter_example = faultline_counter:CounterAdapter`.
The installed package must satisfy the provenance rules in the
[installed-package guide](installed-adapters.md).

The checked boundary is defined by [adapters.py](../../src/faultline/adapters.py):

| Hook | Input and result |
| --- | --- |
| `describe()` | Returns `Description`: boundary/adapter/profile/fixture identities, fixture version and expected record hash, supported operation/fault names, invariant versions, variants, concurrency, authority, quiescence method, registration flag, generator versions, and readiness check IDs |
| `validate(scenario)` | Returns `None`; raises on unsupported domain requests, expectations, identities, or fixtures. Use `scenario.all_operations` so generated requests are included |
| `expand(generator, seed)` (optional) | Required for custom registered generators. Returns exactly `{operations: [...], version: int, algorithm: str}`; operations are ordinary operation dictionaries, version matches `describe().generators[kind]`, and algorithm is a nonempty string of at most 128 characters |
| `prepare(environment, variant)` | Initializes only the owned disposable fixture and returns a checked complete baseline snapshot matching the planned fixture hash |
| `readiness(environment)` | Returns `None` only after a bounded nonmutating health check succeeds |
| `quiesce(environment, timeout_ms)` | Returns `{state: ready|pending|unknown, evidence: [references...]}`; covers all supported work that may outlive a client request |
| `snapshot(environment, role)` | Returns a complete or explicitly incomplete `Snapshot` envelope for `baseline` or `final` |
| `verify(scenario, baseline, final, attempts, *, workload_complete)` | Pure evaluation returning one checked finding for each required invariant/logical-identity pair |

All hooks run within the coordinator's stage deadlines. Preflight discovery, import,
construction, description, and validation have a separate ten-second allowance.
Hook exceptions and malformed results are ERROR. The verifier receives neither
environment nor variant. Do not read mutable state, access the network, or import
application transaction functions from the verifier.

`Description.fixture_hash` is SHA-256 of canonical baseline `records`, not of the
snapshot envelope containing timestamps. `Snapshot` records role, profile, fixture,
authority, isolation, collection times, source transaction, completeness, quiescence,
application-session count, raw records, row counts, and record hash. It must preserve
duplicate/orphan effects and source primary keys. The checked boundary validates
the envelope; the adapter must validate its own record schema and domain semantics.

Findings contain invariant ID/version, logical ID, `PASS`/`FAIL`/`UNKNOWN`, evidence
references, and a failure list with codes, expected/observed values, and source
references. Each failure needs a stable signature. UNKNOWN needs a reason. A
passing subset of required findings is an error, not a passing experiment.

## Generators: write the recipe before cooking

A generator makes a list of ordinary requests. It cannot leave choices for the
scheduler to make later. Declare its name/version in `Description.generators`,
then implement `expand(generator, seed)`. The generator argument contains `id`,
`kind`, `count`, and `parameters`; validate domain parameters inside the hook.
Return exactly `count` operations. Do not use ambient time, network calls, a global
random stream, or process-dependent Python `hash()` to choose values.

The counter's `counter.increments` version 1 takes empty parameters and maps a
SHA-256 counter hash to deltas from 1 through 100. Its `counter-sha256-v1` algorithm
includes seed, generator ID, and index. This is stable fixture generation; modulo
mapping is not unbiased sampling. The bundled `orders.purchases` version 1 expansion
is supplied by the engine; custom kinds use the installed adapter hook.

Faultline checks returned JSON (at most 1 MiB per expansion), version, operation
count, request safety, unique IDs, dependency cycles, attempt totals, and clipped
project ceilings before Docker. Then domain validation sees the explicit and
generated operations together through `scenario.all_operations`. Unknown kinds
are rejected before invoking a generator. Results are detached from adapter-owned
containers and saved as concrete request bytes plus generator metadata. Loading
saved values does not require expanding them again; strict replay is still M3.

See [generator contract tests](../../tests/test_generators.py), which compare
canonical plans across three fresh processes and reject malformed output, cycles,
duplicate IDs, unsafe URLs, and attempts exceeding the project ceiling. The real
generated-counter test builds the external wheel and verifies three database effects.

## Container and helper contract

Supply five locally available images for the application, PostgreSQL, compatible
Toxiproxy, compatible worker, and observer. Every selected image is resolved to an
immutable local image ID before creation. There is no bundled-image-name whitelist
and no automatic pull. The engine fixes network membership, ownership labels,
security restrictions, internal role hostnames, and dependency credentials.

The bundled PostgreSQL template uses an Alpine PostgreSQL image: its writable data
tmpfs is owned by UID/GID 70. A replacement database image must honor that runtime
contract. Application, worker, proxy, and observer run as UID/GID 10001, with dropped
capabilities and read-only root filesystems. Declared ephemeral writable paths are
limited to `/data` or `/cache`; each is a bounded tmpfs, never a host mount. The
worker must implement protocol version 2. The proxy must implement the tested
Toxiproxy control semantics and ports 8474/8666. These are compatibility requirements,
not guarantees for arbitrary image contents.

The application receives `FL_APP_PASSWORD`, `FL_VARIANT`, `FL_DB_HOST=proxy`, and
`FL_DB_PORT=8666`. The database is `faultline` with application role `fl_app`.
The observer receives separate `FL_SETUP_PASSWORD`, `FL_APP_PASSWORD`, and
`FL_OBSERVER_PASSWORD` values for fixture provisioning. After setup, snapshots must
use the read-only observer role. Runtime errors must not expose these values.

Trusted hooks can call:

```python
environment.call(
    "observer", "snapshot", "final", timeout=10,
    module="your_installed_package.storage",
)
```

The engine executes an argv array equivalent to
`python -m your_installed_package.storage snapshot final` inside the owned observer.
The module must already exist in that image. Successful helpers return bounded JSON
on stdout and exit zero. A shell string is never accepted from YAML. The hook must
check any domain-specific acknowledgment before trusting a partial setup. Calls
using the bundled worker HTTP transport have a fixed internal `application` host;
the route selects a declared internal port and confined relative path.

A project supports up to 16 distinct routes and 16 application ports. Route IDs
must be unique. The engine's dependency binding is named `database`; its lifecycle
target is named `application`. These are canonical role bindings even when project
service IDs use other names. Neither fault syntax nor route syntax accepts Docker IDs.

Services can declare literal nonsecret environment values or `{secret_ref: NAME}`
to read a local environment variable. Engine connection/control variables are
reserved. Artifacts retain reference names rather than resolved secret values.
Do not put secrets in literal fields or synthetic request bodies. Per-service
memory/CPU/PID settings and aggregate Docker host capacity are checked before use.

## What this example proves and does not prove

The integration test builds a real wheel and a separate image, runs unsafe/correct
variants and a generated workload through the same engine, checks raw counter totals, and opens the report
without the installed adapter. It tests onboarding without modifying core source.
It does not count as the M4 transfer application, independent correct-B payment
implementation, or broad validation of third-party adapters.

An adapter may be wrong. Validate it against hand-audited records, known defective
applications, and a nearby correct case. A trustworthy readiness endpoint alone
does not establish quiescence. If background work could still mutate state, return
pending/unknown until your declared completion evidence is sufficient.
