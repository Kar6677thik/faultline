# Application adapters and reference domains

## Adapter boundary

An adapter is explicitly installed, trusted Python code conforming to adapter API version 1. The same engine MUST support bundled and external adapters through a registered entry point; it MUST NOT branch on a bundled project's name to determine correctness.

Required capabilities are:

- `describe`: return adapter ID/version, supported engine contract, profiles, operations, generators, fixtures, invariants with versions, required service roles, dependency mappings, and fault compatibility.
- `validate`: validate domain inputs, logical identities, intended outcomes, and required evidence before provisioning.
- `prepare`: initialize schema and seed a fresh environment through the observer helper. Return fixture version/hash and a baseline snapshot; a partial setup is an error.
- `readiness`: bounded nonmutating health checks for the owned services, distinct from fixture validity.
- `quiesce`: after admission stops and faults heal, report `ready`, `pending`, or `unknown` with evidence. It MUST cover work that outlived client timeouts, including background jobs in supported profiles.
- `snapshot`: capture a versioned canonical snapshot and completeness metadata from the declared authority.
- `verify`: pure evaluation of plan, valid histories, baseline, and final snapshot into invariant results. It MUST NOT mutate the AUT, execute network calls, or read ambient mutable state.

Hooks have explicit deadlines and structured diagnostics. Exceptions or malformed returns are ERROR, never FAIL. Cancellation and timeout MUST terminate helper work before teardown. Third-party adapters are not sandboxed plugins; only trusted adapters are supported.

An operation kind declares its request schema, successful/rejection response schemas, expected business effects, and correspondence to authoritative records. Capability negotiation MUST reject unsupported faults or required invariants before dispatch. A report records the installed adapter package version and content hash; modifying an adapter invalidates exact replay compatibility.

## Authoritative observation

For bundled PostgreSQL domains, the observer reads all relevant tables in one read-only `REPEATABLE READ` transaction after quiescence, with a statement timeout. Its credentials cannot modify application state. Setup uses separate write-capable fixture credentials. Row ordering in canonical snapshots is defined by stable primary keys.

A snapshot envelope includes snapshot ID, baseline/final role, schema version, adapter/fixture IDs, authority type, collection start/end, isolation level, completeness flag, included record sets, row counts, source transaction metadata where available, quiescence evidence reference, and content hash. Partial queries MUST NOT be labeled complete. Application response bodies, logs, or trace spans do not substitute for this snapshot.

For bundled services, quiescence is a test capability implemented through an independently observable admission/in-flight registry plus transaction completion checks. Registration MUST precede business mutation, including on error paths; process crashes MUST clear stale ownership only after the database confirms the relevant session/transaction ended. Restarted services may resume only declared background work. The order/transfer profiles have no asynchronous mutation after a handler has deregistered. Acceptance MUST test this contract using a delayed handler that outlives client timeout. Merely sampling identical balances twice is insufficient evidence of quiescence.

Instrumentation required by an adapter MUST be documented to integrators. An application without a trustworthy quiescence signal may still yield a definitive monotonic safety violation, but cannot obtain a full PASS from uncertain final state. v1 does not promise zero-instrumentation onboarding.

## Order profile: `orders_v1`

Entities include accounts, products, orders, wallet ledger entries, inventory movements, idempotency records, and confirmation outbox records. Canonical snapshots MUST preserve all these record sets, including duplicate records; projection MUST NOT deduplicate by purchase ID or silently omit orphan rows.

The standard fixture contains `buyer-a` with opening balance `1000000` paise, `sku-a` priced at `200000` paise, and stock 10. There are initially no orders, ledger entries, movements, or outbox rows. Currency is INR throughout; mixed currency and currency conversion are out of scope. The server calculates price from the product fixture and uses integer arithmetic.

`POST /orders` takes `purchase_id`, `account_id`, `sku`, and positive integer `quantity`, with an `Idempotency-Key` header. Success returns 201 with `order_id`, `purchase_id`, `account_id`, `sku`, `quantity`, `amount_minor`, `currency`, and `status: confirmed`. A correct replay of the same key/payload returns the same status and business response fields; correlation metadata may differ. Profile default desired outcome is one confirmed purchase after bounded retry/recovery, with exactly one debit, one inventory movement, and one outbox intent.

Idempotency keys are scoped by account and endpoint. A canonical payload fingerprint includes account, purchase ID, SKU, and quantity. Same key with a different fingerprint returns 409 `idempotency_conflict`, with no additional side effects. Missing/invalid input returns 422 `invalid_request`; insufficient funds or stock returns 409 with `insufficient_funds` or `insufficient_stock`. The exact error body is `{code, message}`; free-text message is not a correctness discriminator. No test scenario depends on authentication; this is a simulated local service.

The domain requires purchase identity uniqueness within an account independently of the retry token. Reusing a purchase ID with matching content and a different key returns the existing purchase. Reusing it with conflicting content returns 409 `purchase_conflict`. An intentional second purchase uses a new purchase ID. Two concurrent identical purchases must converge on the same result, not two charges.

One successful purchase MUST atomically create the order, debit ledger entry, balance update, inventory movement/stock update, and confirmation outbox intent. Rejection MUST create none of these business effects. Idempotency metadata may record rejection according to the variant's documented retention behavior; its presence does not count as a purchase. There is no real payment call or event broker.

Canonical ledger fields include entry ID, account ID, purchase ID, order ID, debit/credit direction, amount, and currency. Inventory movements include movement ID, SKU, purchase ID, order ID, and signed quantity delta. Orders include immutable business response fields. Outbox records include event ID, purchase ID, order ID, event type, and canonical payload. The observer MUST retain source record IDs so a diagnosis can link to actual rows.

## Reference implementations to specify and later validate

- Correct variant A uses database uniqueness plus a single transaction and row locking for account/product updates. Idempotency acquisition and committed business effects cannot diverge.
- Correct variant B uses a different transaction strategy, such as serializable transactions with bounded serialization retries and immutable purchase result reconstruction. It must independently meet the same observable domain rules; toggling a flag in variant A is not independent validation.
- Bug variants deliberately omit idempotency, race on check-then-insert, commit debit separately from order creation, lose a concurrent balance update, or reuse an idempotency key without payload validation. The delivery document assigns discriminating scenarios to each.

Normal tests MUST pass for the ordinary valid purchase in both correct and defective variants where appropriate. Faultline's known-bad demonstrations must exercise the actual defect, not a backend returning hardcoded failure data.

## Transfer profile: `transfers_v1`

The second application uses accounts, transfers, and ledger entries; no order or inventory schema is present. `POST /transfers` accepts `transfer_id`, `from_account_id`, `to_account_id`, positive `amount_minor`, and INR currency, with an account-scoped idempotency key. Same request/key returns the same 201 result; key or identity conflicts return 409 without side effects. Self-transfer and invalid amount are 422; insufficient funds is 409.

The standard fixture has two accounts with `1000000` paise each. A committed transfer has one debit and one equal credit linked to the same transfer ID, preserves total funds, and cannot overdraw an account. A rejected transfer has no monetary effects. Snapshots include opening/current balances, all transfers, both sides of the ledger, and idempotency records. A deliberately non-atomic transfer variant commits the debit before the credit; controlled restart under a declared test barrier exposes the defect. Such a barrier is explicit adapter instrumentation, not inferred from a log message.

To keep v1 bounded, a test-only lifecycle barrier may pause a fixture application at an adapter-declared checkpoint and acknowledge it over the control protocol. The only bundled checkpoint is `transfer.debit_committed`. Scenarios using this checkpoint are capability-gated and labeled instrumented; generic uninstrumented adapters cannot claim support. Its fault binding extension is specified in the fault document.

## Onboarding acceptance

An integrator supplies a locally built/pinned container image, the manifest, one fixture, readiness/quiescence mechanisms, complete snapshot projection, at least one invariant, and a baseline plus fault scenario. Documentation MUST include the authority limitations and whether source instrumentation is needed. Validation MUST explain missing capabilities before a long experiment begins. An external integration test must demonstrate use of the public adapter contract without modifying the engine.
