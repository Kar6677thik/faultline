# How the experiment engine works

Imagine a teacher running a science experiment. The recipe says which students may
start together, which must wait, and when a piece of equipment should stop working.
The teacher writes down what actually happened and checks the measurements. A
student saying “I finished” is useful evidence, but the measurements decide whether
the experiment succeeded.

Faultline's recipe is a **materialized plan**. Its students are HTTP attempts. Its
equipment is disposable containers. PostgreSQL rows are the independent measurements.
This guide describes the M2 experiment engine; it does not
claim the complete v1 release or deterministic replay. Strict replay, reduction, and the
viewer arrived in M3: see [replay](replay.md), [reduction](reduction.md), and
[the viewer](viewer.md).

## Decide values before starting the experiment

A generator makes concrete operations before any HTTP call. The bundled
`orders.purchases` generator accepts `count` and `parameters.quantity_min` /
`quantity_max` (defaults 1). It uses the seed, generator ID, and operation index as
inputs to SHA-256. The versioned algorithm name is `sha256-counter-v1`. It does not
use a shared random-number stream that concurrent tasks could consume in a different
order. Generated identities are `<generator-id>-<one-based-index>` and quantities
come from the specified inclusive range. This is a repeatable fixture generator,
not cryptographic random sampling or an unbiased statistical benchmark.

Explicit operations come first, then generators in declaration order. Duplicate
IDs and oversized expansions are rejected before provisioning. The saved plan
contains the concrete operations, effective defaults, generator versions, request
body bytes encoded as base64, SHA-256 body hashes, and all possible attempt IDs.
The selected adapter supplies invariant versions and the expected baseline digest.
The observed baseline must match that digest before the workload starts.

External adapters may register their own versioned generators through the optional
`expand` hook. The engine checks their concrete output before admission; see the
[public generator contract](external-adapters.md#generators-write-the-recipe-before-cooking).
This matters because a generator is another input source: it cannot bypass path
validation, dependency checks, or the project's operation/attempt ceilings.

The generated workload still has to fit the fixture: `buyer-a` has enough money for
five units, not an unlimited number of successful purchases. Creating 100 positive
purchases does not make 100 purchases affordable. A correct application can properly
reject them while failing a scenario that demanded impossible successful progress.

Saved values can be reconstructed without running a generator. The historical
`replay` command and exact compatibility checks remain M3 work.

## Admission, completion, barriers, and retries

**Admission** means giving an attempt a concurrency slot. It does not mean its TCP
bytes have reached the application or that its SQL transaction overlaps another.
The scheduler chooses eligible operations in plan order. It never promises that the
operating system or database will finish them in that order.

- `after: [operation_id]` waits until that operation has finished its entire retry
  policy, including unused attempts being marked `not_needed`.
- `after_faults: [fault_id]` waits for a positive application acknowledgment. Merely
  sending a proxy request or asking Docker to kill a process is not enough.
- A barrier releases its members together once their external dependencies and
  fault gates are ready. Only available slots are admitted. Barrier members may
  exceed concurrency; the recorded admission order shows what happened.
- Backoff consumes time but no active HTTP slot. An independent operation may run
  while another waits to retry, even with concurrency one.
- An attempt whose response is being suppressed still holds a slot until its
  timeout. Its upstream success is retained as evidence but the retry policy sees
  `timeout`.

The scheduler rejects cycles, unknown references, inconsistent barrier memberships,
and a fault that depends on an attempt gated by that same fault. If a contingent
trigger never happens, gated operations remain unstarted and the workload cannot
receive a full PASS. Only explicitly listed transport outcomes or HTTP statuses
cause retries. The body and domain headers stay unchanged; correlation IDs change.

Each backoff is between 0 and 300000 ms, but all potential attempts and backoffs
for an operation must fit the scenario's execution budget. A large allowed number
is a ceiling, not a promise that every combination is feasible. Request query
strings cannot repeat a key, including encoded spellings of the same key.

One coordinator owns scheduler state and all history writes. Bounded transport
threads do I/O; they return results through a queue. This keeps disk event order and
ownership changes out of competing threads. It is an implementation choice rather
than a claim that Python threads make applications deterministic.

## One worker process, many correlated requests

Worker protocol version 2 uses one persistent Docker exec stream. It acknowledges
each command ID and returns the correlated result, allowing requests to finish in
any order. The worker supports at most 32 requests in flight. Its Python process
shares loaded modules instead of starting 32 interpreters inside a small container.

The host has one frame reader and a bounded mailbox per outstanding command. Writes
are serialized so JSON lines cannot interleave. A lost stream, malformed frame,
unknown correlation ID, missing acknowledgment, or expired helper deadline is a
harness error. It is never turned into an application timeout or automatically
retried. HTTP has its own total timeout and disables redirect following and hidden
transport retries. Response bodies are capped at 64 KiB.

## Why there are four networks

There are five containers: application, database, proxy, worker, and observer.
Their four internal networks have different jobs:

| Network | Members | Purpose |
| --- | --- | --- |
| Client | Worker, application | Exercise the real HTTP API |
| Dependency | Application, proxy | Force application database traffic through the proxy |
| Storage | Proxy, PostgreSQL, observer | Reach the actual authority |
| Control | Worker, proxy, observer | Configure faults and perform setup |

The proxy's admin server binds to `proxy-control`, an alias installed only on its
control endpoint. The name resolves to that interface, rather than binding admin
to every interface. Its data listener uses port 8666. No ports are published.
The observer deliberately bypasses faults so it can read the database independently.

An initial static-address attempt was rejected by Docker because automatically
allocated networks do not accept user-specified endpoint IPs. The control-only alias
avoids allocating host subnets or choosing addresses manually. Toxiproxy also
normalizes its data listen address to `[::]:8666`; the acknowledgment check accepts
that equivalent wildcard form while checking the binding name, upstream, and enabled
state. The admin interface remains restricted separately and has a real connection
isolation test.

Toxiproxy 2.12.0 is pinned by digest. Latency installs a named toxic and reads back
its exact effective settings; healing removes it and confirms absence. Disconnect
disables the dedicated database proxy and reads back `enabled: false`; healing
enables it and checks again. Duration begins at acknowledgment. These facts prove
configuration, not which SQL statement was affected. The real acceptance test also
checks that disable breaks an established connection and refuses a new connection.

A restart targets only the recorded application container. The controller checks
ownership, sends SIGKILL, confirms exit, waits the declared downtime, starts the
same container, and requires readiness. Its image and resource identity are recorded.
This does not test a host crash or power-loss durability.

One dependency fault per binding and one lifecycle fault per application are
accepted in a scenario. Ambiguous repeated faults on the same target are rejected;
the engine does not invent an ordering. Compatible different-target faults can
compose. Required but unapplied faults prevent PASS. An action or healing failure
is ERROR even if a business violation is independently known.

## Knowing whether work really ended

A client timeout is like leaving a shop before hearing whether a purchase succeeded.
It does not stop the cashier. Faultline must wait for the cashier to finish before
checking the till.

The order handler commits an `inflight` registration before business mutation. It
records a process-epoch UUID and the database session's PID **and start time**;
PID alone could be reused. The registry connection stays open while the handler
may mutate. The handler removes its row only after transaction work ends. If its
connection breaks, the row remains as uncertain evidence rather than claiming
completion.

Drain records the registry, gracefully stops the application, confirms process
exit, and independently checks database sessions and registry session ownership.
Stale registration rows are cleared only after the owned process has exited and
PostgreSQL confirms zero application sessions. This clears diagnostic rows, never
business rows. A forced termination or exceeded drain budget cannot establish a
full PASS. A delayed-handler test waits for a real purchase that outlives the client
timeout; a crash test verifies stale reconciliation and bounded termination.

The public adapter hook reports `ready`, `pending`, or `unknown` with evidence
references. Pending is polled within the drain deadline. An adapter with background
work must provide its own trustworthy completion mechanism; the bundled synchronous
handler protocol is not a universal background-job detector.

## Check every purchase without hiding shared-state mistakes

The verifier groups by logical purchase, not by transport token. Three duplicate
requests should converge on one purchase. Three independent purchases should leave
three orders and three debits. The balance and stock equations still use **all**
observed ledger/movement rows. Filtering by one purchase must not hide an unrelated
orphan effect, unknown request, or orphan idempotency record.

Each logical identity gets all seven required findings. The checked adapter refuses
a missing or duplicate `(invariant_id, logical_id)` pair. Expected rejection cases
check both the matching response and absence of new forbidden effects. Temporary
503 responses from interrupted database connections are eligible for explicit retry;
they do not prove rollback. The final authoritative rows determine completion.

## Where to read the implementation and tests

| Behavior | Source | Tests |
| --- | --- | --- |
| Strict inputs and materialization | [models.py](../../src/faultline/models.py) | [test_scheduler.py](../../tests/test_scheduler.py), [test_scenarios.py](../../tests/test_scenarios.py) |
| Admission and conditional retries | [scheduler.py](../../src/faultline/scheduler.py) | [test_scheduler.py](../../tests/test_scheduler.py), [test_runner.py](../../tests/test_runner.py) |
| Multiplexed transport | [worker.py](../../src/faultline/worker.py), [remote.py](../../src/faultline/remote.py) | [test_worker.py](../../tests/test_worker.py) |
| Isolation and ownership | [environment.py](../../src/faultline/environment.py), [ownership.py](../../src/faultline/ownership.py) | [test_integration.py](../../tests/test_integration.py), [test_creation_recovery.py](../../tests/test_creation_recovery.py) |
| Fault acknowledgments | [faults.py](../../src/faultline/faults.py), [proxy.py](../../src/faultline/proxy.py) | [test_faults.py](../../tests/test_faults.py), real fault matrix in integration tests |
| Registry and independent grading | [orders.py](../../src/faultline/orders.py), [verifier.py](../../src/faultline/verifier.py) | delayed-handler, concurrent-purchase, rejection, and independent verifier tests |

The [decision log](decisions.md) records alternatives and limitations. Acceptance
measurements belong in a separate results document; passing unit tests alone does
not establish a real network-failure guarantee.
