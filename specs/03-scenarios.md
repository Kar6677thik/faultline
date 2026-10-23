# Project and scenario contracts

## Document formats and strictness

Project and scenario files use UTF-8 YAML with a JSON-compatible value model. The loader MUST reject duplicate keys, unknown fields, custom tags, anchors/aliases, nonfinite numbers, and values that fail strict types. Strings that resemble numbers MUST NOT be coerced to integers. Documents have a 1 MiB size limit and a nesting limit of 32. Paths are resolved relative to the project file, normalized, and constrained to the project root unless the field explicitly describes the artifact output directory.

Both documents require `schema_version: 1`. IDs match `[a-z][a-z0-9_-]{0,63}` and are unique within their kind. Secrets, arbitrary shell commands, arbitrary URLs, Python expressions, and template evaluation are not scenario syntax.

## Project manifest

The default name is `faultline.project.yaml`. Required top-level fields are `schema_version`, `project_id`, `adapter`, `services`, and `routes`. Optional fields are `variants`, `scenario_paths`, and `limits`. `adapter` identifies an explicitly installed, trusted Python entry point and adapter contract version; importing it is authorized only by a local run/validation command, never by viewing artifacts.

Each service declares a unique `id`, immutable `image` reference or locally resolved image ID, a `role` (`application`, `postgres`, `proxy`, `worker`, or `observer`), allowed internal ports, resource limits, nonsecret environment values or secret references, and readiness capability. The engine assigns networks, labels, volumes, and user/capability restrictions. It MUST NOT accept user-provided container IDs, host networking, host PID namespaces, host bind mounts, privileged mode, extra capabilities, published ports, or a Docker socket mount.

Each route maps a symbolic `id` to an application service, internal port, and base path. An operation uses a route ID and relative path; it cannot supply an absolute URL. Allowed paths MUST begin with a single `/`, contain no fragment, authority, traversal, or encoded equivalent, and remain under the route's base path after normalization. Redirect following is disabled. No runtime request may resolve an arbitrary host.

A dependency binding declared by the adapter maps the application database connection to the engine-managed proxy and PostgreSQL instance. Variants select a fixed application image and adapter-defined configuration, without changing fixture identity or invariant semantics. All effective service settings are captured in the environment manifest with secrets removed.

Only the order and transfer profiles are bundled. An external adapter may support another HTTP/JSON application and a PostgreSQL authority using these roles. Arbitrary service graphs and additional database engines are deferred.

### Nested manifest records

To avoid implicit configuration APIs, schema version 1 uses these exact field names:

- `adapter` is an object with required `entry_point` (installed Python entry-point name), `contract_version` (1), and `package_version` (exact version string). The resolved package content hash is captured at validation.
- `services` is a nonempty list. Each entry requires `id`, `role`, `image`, `ports`, `resources`, and `readiness`; optional fields are `environment` (empty by default) and `writable_paths` (empty by default). v1 allows exactly one application, PostgreSQL, proxy, worker, and observer service. The bundled project templates declare all five; worker/proxy/observer images must be compatible with the installed engine. Service entrypoints come from trusted images, never command text in configuration.
- `ports` is a list of distinct integers in 1–65535, exposed internally only. The adapter identifies their named purposes. The engine assigns network addresses; callers cannot request host ports or network attachments.
- `resources` requires `memory_bytes`, `cpu_millis` (1000 is one CPU), and `pids_limit`, all positive integers. Per-service allocations and their aggregate must satisfy host and project budgets. Database storage and each allowed `writable_paths` entry use engine-owned ephemeral volumes or tmpfs; absolute container paths are validated and cannot name host paths.
- `readiness` requires `check` (an adapter-registered nonmutating check ID). Optional `timeout_ms` defaults to 10000 and `poll_ms` to 250; both are positive, bounded by the provisioning budget. Checks cannot be arbitrary manifest shell commands.
- `environment` maps variable names to literal strings or objects containing only `secret_ref` with an environment-variable name. Engine-owned connection/control/correlation variables are reserved and cannot be overridden. Dependency credentials are generated per run and injected by the engine.
- `routes` is a nonempty list of objects requiring `id`, `service`, `port`, and `base_path`. The service must have application role and declare that internal port. A base path defaults only when explicitly supplied as `/`; it is not inferred from an external URL.
- `variants` is a map from variant ID to an object with required `application_image` and optional `environment` using the same value rules. Only application settings may change. If variants are absent, the application's declared image defines a single variant named `default`. All dependency images and contracts remain fixed unless an explicit comparison records their differences.
- `scenario_paths` is a list of project-relative file paths used for documentation/discovery; commands still select a specific scenario. It does not cause automatic execution.

`limits` in either document accepts only `execution_ms`, `provisioning_ms`, `drain_ms`, `observation_ms`, `verification_ms`, `cleanup_ms`, `total_ms`, `artifact_bytes`, `max_operations`, `max_attempts`, and `max_faults`. Defaults and hard maxima are defined below and in the safety contract. Project limits set local ceilings; scenario limits choose values within those ceilings. An omitted project ceiling uses the engine hard cap, and an omitted scenario value uses the documented default clipped to the project ceiling. `concurrency` remains a scenario top-level field. Contradictory deadlines, such as a total budget shorter than reserved cleanup, are invalid. Host CPU/memory constraints are enforced through `services.resources` and preflight, not undocumented scenario fields.

## Scenario example

This normative shape describes the first milestone. IDs and numeric values are examples; the semantics are fixed.

```yaml
schema_version: 1
id: lost_response_purchase
adapter_profile: orders_v1
fixture: standard_wallet
seed: 42
concurrency: 1
operations:
  - id: purchase_a
    kind: create_order
    logical_id: purchase-a
    route: orders_api
    request:
      method: POST
      path: /orders
      headers:
        Idempotency-Key: purchase-a-key
      json:
        purchase_id: purchase-a
        account_id: buyer-a
        sku: sku-a
        quantity: 1
    retry:
      max_attempts: 2
      on: [timeout, connection_error]
      backoff_ms: [100]
    timeout_ms: 1000
faults:
  - id: hide_first_response
    kind: response_suppress
    target: orders_api
    trigger:
      event: upstream_response_complete
      operation_id: purchase_a
      attempt: 1
      status: 201
    required: true
invariants:
  - orders.at_most_one_debit
  - orders.purchase_atomicity
  - orders.wallet_conservation
  - orders.inventory_conservation
  - orders.confirmation_intent
  - orders.idempotency_contract
  - orders.completed_purchase
limits:
  execution_ms: 30000
  drain_ms: 10000
  artifact_bytes: 52428800
```

The order adapter derives the ₹2,000 price from its fixture; the client does not choose the charge amount. Baseline wallet funds are ₹10,000. Two committed unsafe attempts would therefore produce ₹4,000 of debits, and the correct variant ₹2,000. Actual results MUST be measured, never inserted from this illustration.

## Scenario fields

Required fields are `schema_version`, `id`, `adapter_profile`, `fixture`, `seed`, `operations`, and `invariants`. Optional fields are `concurrency` (default 1), `faults` (empty), `barriers` (empty), `limits`, and `generators` (empty). `seed` is an unsigned 64-bit integer. `invariants` MUST be a nonempty list of adapter-registered IDs; unknown IDs are invalid. Fixtures and operation kinds MUST be adapter-registered.

Every operation requires `id`, `kind`, `logical_id`, `route`, and `request`. Optional fields are `after`, `after_faults`, `barrier`, `retry`, `timeout_ms`, and `expectation`. `after` is a list of operation IDs whose attempts must all reach terminal disposition before this operation becomes eligible, irrespective of transport success. `after_faults` is a list of fault IDs whose application must be acknowledged before admission; it defaults to empty. Default `after` is empty. Concurrency 1 serializes attempts, but a later independent operation may run while an earlier operation waits for retry backoff. Require an `after` dependency when whole-operation ordering matters. All operation/fault dependencies MUST be acyclic.

`request` requires `method`, `path`, and `json`; `headers` is optional. v1 methods are GET, POST, PUT, PATCH, and DELETE, with JSON-compatible bodies (including null). Reference profiles use only strings, booleans, null, and signed 64-bit integers in payloads; fractional money is invalid. Repeated query keys, binary bodies, streaming HTTP, WebSockets, and multipart requests are unsupported. Headers are case-insensitive string keys with string values; duplicate case-folded keys are invalid. The adapter validates the operation/request against its domain contract. The engine owns `X-Faultline-Run-Id`, `X-Faultline-Operation-Id`, and `X-Faultline-Attempt-Id` and MUST reject attempts to overwrite them.

`expectation` defaults to the adapter's declared operation outcome. It may select `success` or an adapter-named rejection such as `insufficient_funds`, `invalid_quantity`, or `idempotency_conflict`. Required success is checked separately from safety: doing nothing is not a successful purchase. Expected invalid operations may finish with the declared rejection and MUST have no forbidden side effects.

`logical_id` is a domain intent identity, not an idempotency token or HTTP attempt ID. Multiple operations may intentionally share a logical ID, for example to test duplicates. The adapter MUST reject incompatible payloads for the same logical identity unless this is an explicit conflict test. Each operation has attempt IDs `<operation-id>/1`, `<operation-id>/2`, etc.; the run UUID makes these globally distinct.

## Retry contract

Default `retry` is `max_attempts: 1`, empty `on`, and empty `backoff_ms`. The allowed maximum is 5 attempts per operation. `on` contains transport outcomes `timeout`, `connection_error`, or exact HTTP statuses `http_429`, `http_500`, `http_502`, `http_503`, `http_504`. There are no library-level hidden retries. A malformed response is recorded as `invalid_response` and is not automatically retried in v1.

`backoff_ms` has exactly `max_attempts - 1` nonnegative integers. Backoff starts after the preceding client-visible outcome. Retry eligibility is conditional on that outcome being listed in `on`; otherwise the remaining planned attempts are `not_needed`. Retried attempts MUST preserve logical identity, idempotency key, method, path, body bytes, and domain-relevant headers. Per-attempt correlation headers change. A conflicting payload test is a separate explicit operation, never an implicit retry.

The timeout is a total attempt deadline measured from dispatch, covering connect, send, response body, and response suppression. Default is 2000 ms, permitted range 50–30000 ms. Client timeout does not imply server cancellation or rollback. The adapter's drain/quiescence contract must account for this.

## Barriers, generators, and triggers

A barrier has `id` and `members`, where members are distinct operation IDs. Each member names that barrier in its `barrier` field; membership must agree in both directions. Each operation belongs to at most one barrier. Members MUST NOT depend on one another directly or transitively. Once all external operation dependencies terminate and all members' fault gates are acknowledged, the scheduler records one release event and makes all members eligible. Membership may exceed concurrency, but the report MUST show actual admission order. A barrier is not a claim of overlapping database transactions.

Generators are bounded adapter-registered expansions. Each declares `id`, `kind`, positive `count`, and validated `parameters`. They may choose fixture-backed accounts, SKUs, amounts, or duplicate identities; they do not execute code from YAML. Generator output is fully materialized before execution, appended in generator declaration order after explicit operations, and included in the plan. Generated IDs MUST be collision-free. The engine stores generator name/version, PRNG algorithm/version, and all resulting values. Replay uses these values rather than regenerating them. Identical generator inputs and pinned versions MUST yield identical plans, regardless of asynchronous completion order.

Fault triggers are either an event binding or an execution offset. Event bindings specify `event`, `operation_id`, `attempt`, and optional `status`. Supported events are `attempt_dispatched`, `upstream_response_complete`, `client_outcome`, and the capability-gated `adapter_checkpoint`. The latter also requires `checkpoint: transfer.debit_committed` and is allowed only for the instrumented transfer restart experiment defined in the fault contract. Only `upstream_response_complete` accepts `status`; for response suppression the exact successful status is required. Offset triggers specify `offset_ms`, relative to worker execution start. A trigger has exactly one form and fires at most once. Every action records intended eligibility and actual application time.

Faults require `id`, `kind`, `target`, `trigger`, and `required` (default true). Additional parameters and target types are defined by fault kind in [fault semantics](05-faults.md). Invalid targets, impossible attempt indices, unsupported event/kind combinations, negative durations, and incompatible overlapping faults MUST fail validation. A trigger whose contingent attempt is never dispatched is valid but unapplied; a required such fault prevents PASS.

## Limits and plan

Default concurrency is 1; a concurrent scenario must explicitly set a value from 2 through 32. Hard caps are 1000 logical operations, 5000 potential attempts, 32 faults, and 32 in-flight attempts. Plans may lower these limits but v1 MUST reject higher values. Default execution deadline is 60000 ms; maximum is 300000 ms. Provisioning has a separate default 60000 ms deadline, drain 10000 ms, observation 10000 ms, verification 10000 ms, and cleanup 30000 ms. These default stage deadlines may be lowered or increased up to 300000 ms each; the total run wall budget defaults to 300000 ms and has a 900000 ms hard maximum. The total budget reserves cleanup time and may shorten earlier stages. Verification timeout is a harness ERROR, not an invariant violation.

The plan contains every effective default, operation and potential attempt, body bytes/hash, dependency, barrier, fault rule, fixture digest, invariant IDs and versions, adapter contract identity, schema/engine/fault semantics versions, seed, generator outputs, and resource budget. Its canonical hash excludes run UUIDs, timestamps, and absolute artifact paths. The hash uses UTF-8 JSON with sorted object keys, no insignificant whitespace, and only integers for numeric configuration; body bytes are base64 encoded. The JSON serialization/canonicalization version MUST be recorded. Actual run IDs, selected variant, installed engine/adapter package hashes, image digests, and environment resources belong to the run manifest, not the reusable plan. This separation allows the identical workload/fault plan to be compared against a corrected application image while recording changed environment identity.

Validation includes static validation before provisioning and dynamic validation of capabilities/readiness/baseline afterward. Dynamic failure MUST produce an artifact and ERROR. Static failure produces a structured diagnostic and no experiment resources.
