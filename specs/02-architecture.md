# Architecture and stack

## Technology decisions

- Python 3.12+ for CLI, engine, worker, reference services, and adapter API. Use `asyncio` for bounded I/O concurrency, HTTPX for HTTP transport, Pydantic v2 for strict validation, Typer for CLI, and FastAPI for reference APIs and the local report server.
- PostgreSQL 17 as the reference transactional database. Use psycopg 3 and explicit SQL where transaction behavior matters. Application variants may choose different transaction techniques while meeting the same domain contract.
- Docker Engine with Linux containers and the Python Docker SDK for run-owned lifecycle. Toxiproxy supplies network impairment. The core will create resources directly from the constrained manifest; arbitrary Compose files are not an input contract.
- React 19, TypeScript, Vite, and Node.js 24 LTS for the report viewer build. The release package MUST serve compiled assets; viewing a report MUST NOT require a frontend development server or Node.js installation.
- JSON and NDJSON bundles as authoritative tool records. A local SQLite catalog MAY index runs; it MUST be rebuildable from artifacts and MUST NOT determine verdicts. No separate tool database service is required.
- pytest for meaningful contract/integration tests and property-based testing for scheduler, validation, and reduction logic where useful. Browser acceptance tooling is an implementation choice.
- Structured run events are mandatory. OpenTelemetry traces are optional supplementary evidence after the core works; they never replace business snapshots.

Patch versions, image digests, the Python environment, and the frontend lockfile MUST be pinned when implemented. The chosen Python minimum is a compatibility floor, not a claim that it is the newest Python release.

## Supported hosts

The supported execution path is Linux x86-64, including Ubuntu in WSL2 on Windows with Docker Desktop's Linux engine. CI uses Linux with a local Docker daemon. Native Windows process fault injection, Windows containers, rootless Docker, remote Docker contexts, macOS, and ARM hosts are not release-blocking support targets. Unsupported host configurations MUST fail preflight clearly rather than receive partial isolation guarantees.

The React viewer may run in a modern Chrome, Edge, or Firefox browser on Windows or Linux. The initial acceptance resource budget is four logical CPU cores, 8 GiB RAM, and 10 GiB free disk. This is a target to measure, not an established requirement from benchmarks.

## Component boundaries

The host CLI owns validation, planning, Docker orchestration, history persistence, verification, replay/reduction jobs, and report generation. It alone has access to the local Docker socket. It MUST NOT expose that socket to workload, AUT, proxy, observer, or viewer containers.

A per-run Python worker executes the materialized workload and talks to the Toxiproxy administration endpoint inside the run's private control network. It receives a structured plan and emits structured events over a bounded Docker execution stream. Requests and acknowledgments use request IDs; control commands that may be retried MUST be idempotent by command ID. The protocol is versioned with the engine and MUST reject mismatched workers. EOF, worker exit, or a missing acknowledgment is an explicit harness failure, not successful completion.

A trusted observer/fixture helper reaches PostgreSQL directly, initializes the reference state, and reads authoritative snapshots. It is separate from AUT request handling. Adapter hooks can execute in this helper using the same versioned worker protocol. The coordinator persists every event before acknowledging critical control milestones; faults are not considered applied on the basis of a command send alone.

The verifier consumes validated plans, histories, and snapshots. It MUST NOT query mutable AUT state after teardown. The reducer invokes the ordinary run lifecycle and evaluates a fixed violation signature. The viewer consumes artifacts through a read-only local API and MUST NOT import project adapter code.

## Network topology

Each run has four private, Docker `internal` networks with unique run-owned names:

- Client network: workload worker and AUT HTTP service.
- Dependency network: AUT and proxy data listener.
- Storage network: proxy database connection, PostgreSQL, and observer helper.
- Control network: worker, Toxiproxy admin listener, and observer helper.

The AUT cannot resolve or directly connect to PostgreSQL on the storage network and cannot reach proxy administration on the control network. Toxiproxy admin MUST bind to its control-network address, not every interface. The AUT database URL MUST point to the proxy data listener. The observer bypasses database faults on purpose and never uses application read APIs as its only authority.

No AUT, database, or proxy port is published on the host. Images are pulled/built before the isolated run starts. No external network is required during execution. Docker execution streams provide host coordination without exposing control ports. Image construction is trusted user activity, outside the experiment's network isolation boundary.

## Lifecycle state machine

The normal lifecycle is `created -> validating -> provisioning -> ready -> executing -> draining -> observing -> verifying -> finalizing -> completed`.

1. Validate versions, platform, resource budget, adapter, image availability, scenario bounds, and target capabilities. Resolve images to immutable digests and expand the complete plan.
2. Allocate a run ID, create the artifact directory exclusively, persist the plan and environment manifest, then provision labeled resources.
3. Wait for bounded readiness, apply schema/fixtures, and capture the baseline. A failed baseline check MUST prevent workload execution.
4. Execute the plan and fault controllers, recording both eligibility and actual dispatch/application events.
5. Stop admitting work. Heal active faults and ensure restarted processes are ready. Complete admitted attempts within the drain deadline; never retry implicitly during drain.
6. Establish adapter quiescence and capture a coherent final snapshot. If quiescence is not reached, report the limitation using the verification rules.
7. Verify and write a draft result. Cleanup runs in a finally-equivalent path even if any earlier stage failed. Finalize the report with cleanup and harness status using an atomic write.

Failed stages proceed to best-effort evidence collection and finalization; they do not skip cleanup. Cancellation stops new work, attempts to heal faults, collects whatever valid evidence remains, cleans resources, and produces an interrupted result. A cleanup failure makes the run ERROR even if invariant violations were also found; violations remain visible.

A sudden coordinator crash can leave a nonterminal bundle. Recovery MUST preserve it, mark it interrupted/error, and offer scoped cleanup. It MUST NOT invent terminal history or silently resume a partly executed scenario.

## Concurrency and scheduling

Default concurrency is one run per command and one active run per local workspace, enforced by an ownership lock with process identity. Distinct CI jobs may run independently; UUID resource names prevent collision. v1 does not provide a distributed scheduler or shared installation service.

Within a run, concurrency counts active HTTP attempts, including attempts whose responses are being suppressed. Eligible actions are admitted in deterministic plan-index order while capacity is available. Barrier release makes several attempts eligible together; it does not promise simultaneous socket writes. Unplanned task scheduling differences are captured as observations.

All durations use worker monotonic time for workload and worker-managed faults, and coordinator monotonic time for Docker actions. Only per-source monotonic ordering and explicit control acknowledgments establish happens-before relations. Coordinator receive order is useful for display but does not prove distributed causality.
