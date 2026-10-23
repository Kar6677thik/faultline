# Engineering principles for Faultline

The user supplied *The Software Engineering Field Guide* on 2026-10-23 as engineering guidance. Apply its central rule: choose the least complicated solution that satisfies the actual requirements, understand its failure modes, and verify it. Its catalog is a set of design options, not a requirement to introduce every pattern. Python and React remain the agreed stack; the guide's illustrative .NET examples do not change that decision.

## Principles applied in the first implementation

- **Risk-driven incremental delivery:** prove the duplicate-charge experiment with actual PostgreSQL before building the UI, plugins, or a general scheduler.
- **Separation of concerns:** strict configuration lives in `models`; application transactions in `orders`; authoritative checks in `verifier`; Docker ownership in `environment`; scheduling in `engine`; evidence in `artifacts`; presentation in `cli`. The verifier does not import application transaction code or inspect the variant name.
- **Simple composition:** one Python package with focused modules. Disposable processes exist to isolate the experiment, not to turn the tool into independently deployed microservices. Use ordinary functions and explicit transport/clock dependencies rather than speculative strategy hierarchies.
- **Transaction boundaries and operation identity:** the correct reference implementation uses account-row locking, database uniqueness, and one transaction covering order, money, inventory, outbox intent, and idempotency metadata. Business identity and transport attempt identity remain distinct. A timeout cannot establish rollback.
- **Bounded work and fail-fast contracts:** configuration rejects unsupported features, unknown fields, unsafe destinations, invalid retries, and ambiguous identities. Requests have total deadlines and body limits; retry count and backoffs are explicit. The fixed backoff is a controlled experiment input, not a production retry policy recommendation.
- **Least privilege and ownership:** no published application ports, host binds, Docker socket in child containers, or privileged containers. Verify run labels plus recorded resource IDs before cleanup. The observer connects through read-only database credentials. Fixtures contain synthetic data.
- **Evidence and conservative outcomes:** persist correlated events, fixture and final records, image identities, and measured outcomes. Separate FAIL from ERROR and INCONCLUSIVE. Never reinterpret a failed harness as application resilience.
- **A balanced test portfolio:** independent hand-audited verifier fixtures plus real Docker/PostgreSQL tests, corruption cases, timeout cases, and cleanup checks. Assert behavior and source records, not only mocked method calls or successful HTTP status.
- **Reproducible dependencies:** commit `uv.lock`, pin base/database image digests, and record resolved runtime image IDs. A passing generated snapshot is not a substitute for an independent expected result.
- **Explicit limitations:** record milestone shortcuts and their replacement conditions in [implementation status](implementation-status.md). Do not silently relax the release specification or claim guarantees that have not been measured.

## Tradeoffs to preserve

Account locking serializes purchases for an account. That is acceptable for the tiny correctness fixture; throughput optimization requires measurements and additional concurrency tests. The deliberately unsafe variant bypasses duplicate protection so Faultline has a genuine positive detection case.

The outbox is a durable event intent only; no broker is introduced until delivery semantics are actually tested. Recorded histories and snapshots are file artifacts, not a reason to add another database service.

Milestone 1 establishes quiescence by gracefully stopping the fixture application and checking that its database sessions have ended. This is simpler and stronger for the bounded synchronous fixture than guessing completion from repeated balances, but it does not support testing continuously running background workers. That needs the later adapter quiescence contract.
