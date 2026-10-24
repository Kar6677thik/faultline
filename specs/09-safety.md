# Safety and operations

## Trust and threat model

Faultline v1 is a local tool for trusted application images, trusted adapters, and synthetic fixture data. It is designed to prevent accidental targeting of existing systems and contain normal test failures. It is not a security sandbox for malicious code. Docker access is powerful host access; a hostile image, adapter, or local Docker administrator is outside the containment guarantee.

Remote production endpoints, real payment credentials, live customer data, and user-selected existing containers are unsupported. There is no `--unsafe` option that bypasses target ownership. Safety validation is mandatory in CLI and CI, not a UI preference.

## Resource ownership and target authorization

Before mutation, the coordinator MUST verify both the run manifest's resource ID and Docker labels identifying Faultline, project ID, run UUID, and a per-run ownership nonce. Names or user-supplied labels alone are insufficient. Re-resolve and verify ownership immediately before kill, restart, disconnect, volume deletion, network deletion, or cleanup operations.

The engine creates each container, ephemeral volume, and private network, records the returned IDs, and only then may expose them as run targets. An unavailable ownership record means refusal, not broader discovery. `clean --stale` scans only recorded resources from known local bundles and checks that the process/lock is no longer live; it MUST NOT delete unrelated resources found by a loose name prefix.

Creation recovery MAY persist an intent before the create request, containing an exact generated name, allowed kind/role, and unique per-creation token. An unresolved intent authorizes only read-only inspection of that exact name. Reconciliation MUST verify the exact name, all run/project/nonce labels, and matching creation-token/kind/role labels, then durably bind the full immutable ID before any mutation. Later cleanup MUST reuse that binding, never rebind a known ID by name. Recovery bindings outside the original bundle MUST identify the exact original manifest and preserve its evidence. A currently absent unresolved creation is uncertain, not proof that an interrupted daemon request cannot complete later. Legacy missing IDs without a recorded intent remain unrecoverable automatically.

Use a local Docker daemon only. A nonlocal Docker host/context fails preflight. The controller MUST not mount user directories or the Docker socket inside the AUT. Run containers have non-root users where supported, dropped Linux capabilities, no privilege escalation, and read-only root filesystems except explicit run-owned writable locations. PostgreSQL's required writable volume is ephemeral and run-owned. An image incompatible with these restrictions is rejected with a diagnostic; user-specified privileged exceptions are outside v1.

The managed topology has no published AUT ports and no external egress. AUT dependencies must route through the declared proxy; reachability checks verify that the application cannot bypass the proxy to reach storage or control endpoints. Internal networking is defense against accidental external access, not protection from a compromised Docker daemon.

## Cleanup and recovery

On every ordinary exit, cancellation, deadline, controller error, and adapter error, stop admitting operations, attempt to heal faults, stop owned application/worker/helper containers, and delete owned containers, volumes, and networks in dependency order. Capture sufficient state before deletion when possible, subject to deadlines. Do not retain database volumes by default.

Cleanup is idempotent: an already absent recorded resource is success; an ID whose ownership no longer matches is a refusal and ERROR. Cleanup has its own deadline, is included in the total run budget, and reports every unresolved resource ID. On a hard process kill or machine restart, a later invocation diagnoses stale runs and instructs the user to use scoped cleanup; it does not silently delete resources during a read-only report operation.

Artifacts are preserved until the user deletes them. v1 has no automatic artifact pruning and no command that recursively deletes arbitrary artifact paths. Retaining failed resources for interactive debugging is deferred; reproducible reruns and snapshots are the supported investigation path.

## Resource and time bounds

Hard ceilings per run: 32 in-flight attempts, 1000 logical operations, 5000 potential attempts, 32 faults, 50 MiB total artifacts, 64 KiB request or response body per attempt, and 5 MiB logs per service within the overall artifact cap. Default artifact and count budgets equal those ceilings; scenarios may lower them. Concurrency defaults to 1 as specified in the scenario contract. Limits apply before dispatch and during recording. Oversized requests are rejected before execution; response reading stops at the body ceiling with `invalid_response`, so an unbounded response cannot consume unlimited memory or trigger complete-response suppression. The fixture adapters must bound snapshot row counts; exceeding the artifact budget is ERROR, while a completed observation explicitly reporting incomplete authority yields UNKNOWN for affected checks. Neither can become a silently truncated complete snapshot.

For the supported reference environment, set aggregate container memory limits no greater than 4 GiB by default and a CPU budget no greater than four logical CPUs; report individual allocations. Disk headroom and artifact budget are checked before provisioning. OOM exits are retained as observed service failure; if the harness/observer is OOM-killed, the outcome is ERROR. Unexpected AUT exit is part of application behavior, but no complete verdict is possible without valid recovery and evidence.

Repeat/reduction jobs default to a 1 GiB total artifact budget. The original source failure bundle is outside the new job's budget and remains untouched. Before starting a trial, reserve its maximum artifact allowance and cleanup headroom; stop the group with an explicit budget termination when insufficient space remains. Never evict evidence silently to continue searching. This may stop a default 50-trial reduction early; the result reports the actual reason and counts.

Stage and total deadlines are defined in the scenario contract. No workload, fault controller, observation query, Docker wait, or shutdown wait may be unbounded. Resource exhaustion MUST not be translated into a business correctness failure without sufficient independent evidence.

## Secrets and evidence

Use synthetic fixture data. Secret fields in project configuration refer to local environment-variable names and are resolved at runtime; plaintext secret values in manifests are invalid. Docker connection credentials, database passwords, and authorization/cookie headers MUST NOT be serialized into plans, logs, reports, diagnostics, or frontend data. Redact before persistence, not just before display.

The plan stores allowed nonsecret request fields and secret references. Authentication headers may be provided through declared secret bindings, but cannot carry logical business identity. Their values are intentionally excluded from reusable plan hashes and response evidence. They must be available on replay, and the report records whether a binding was resolved without storing a hash of the secret itself. Fixture business data and request payloads are synthetic and replayable; a required business field that must be redacted makes strict replay unavailable. Report that incompatibility instead of silently changing inputs.

Response/log capture uses a default allowlist for structured reference-service fields. Authorization, cookies, tokens, passwords, connection strings, and adapter-declared sensitive fields are excluded. Unstructured logs are disabled by default for third-party adapters until a sanitizer is declared. A digest or bounded preview cannot substitute for a complete authoritative record needed by an invariant. If redaction removes essential verification evidence, the affected invariant is UNKNOWN and the limitation must be explicit.

No telemetry, remote analytics, or automatic artifact upload is enabled in the local tool. CI artifact upload is an explicit repository workflow step with a stated retention period. Artifacts can still contain synthetic identities and developer paths; users should be able to inspect the bundle before sharing.

## Viewer and artifact input

Artifacts are untrusted data when inspected, even when their original adapter was trusted. Apply size/depth limits, strict parser safety, and normalized path checks. Reject symlinks/reparse points that escape the selected root, traversal components, absolute paths in an inventory, unexpected active HTML, and archives. Never deserialize Python pickle or execute embedded scripts, SQL, or shell text.

The local API token, loopback binding, strict Host/Origin checks, disabled CORS, and a restrictive Content Security Policy protect against a malicious website reading local results. Render all AUT strings as text. Raw Markdown/HTML from reports or logs MUST NOT be inserted into the DOM. External links are plain user-activated links with safe URL schemes; no automatic image or resource fetch from artifact content is permitted.

The viewer root has read-only access to chosen artifacts. Artifact IDs map to validated inventory entries. Known file size and hash are checked before serving completed evidence; malformed/incompatible bundles show an error rather than an invented verdict. Neither the API nor React may import adapters or accept a command to execute a scenario.
