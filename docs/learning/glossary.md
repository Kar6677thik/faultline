# Faultline glossary in plain language

**Adapter:** a translator between the test engine and an application's business rules. It says how to prepare data, stop writes, read state, and judge results. A checked order bridge supports the built-in or a separately installed trusted package; the complete public API remains planned. See the [adapter guide](adapters.md).

**Entry point:** an installed package's address card mapping a short name to a Python factory. Faultline looks up the selected name without importing unselected adapter modules. See the [installation guide](installed-adapters.md).

**Creation intent:** a record saved before a Docker create request, including the exact name, job, kind, and unique creation token. It permits narrowly scoped inspection if the returned ID is lost; it does not itself authorize deletion.

**Recovery binding:** a durable connection between a recorded creation intent and the resource's full immutable Docker ID. Later cleanup reuses this ID rather than looking up a possibly reused name. See [creation recovery](creation-recovery.md).

**Wheel and RECORD:** a wheel is an installable Python package archive. Its RECORD is the file list used to inspect the installed package. Faultline hashes actual bytes and refuses missing measured files rather than trusting that list's declared hashes.

**Capability:** an explicit declaration of what an adapter can handle, such as an operation, fault, fixture, or invariant version. Unsupported requests are refused before provisioning.

**Hook:** a named function the engine calls at a particular stage, such as preparing the fixture or checking final state.

**Source provenance:** a record of which package version and source-file fingerprint produced a result. It helps identify the judge; it is not a digital signature or proof of correctness.

**Project / scenario:** the project describes the laboratory's services, application images, and limits; the scenario describes the operations and failures to try inside it. The supported manifest is explained in the [project guide](projects.md).

**Ceiling / default / explicit value:** a ceiling is the largest permitted value, a default is used when you omit a setting, and an explicit value is what you actually wrote. Project ceilings may clip omitted defaults; explicit values above the ceiling are rejected.

**MiB / CPU millis / PID limit:** MiB is 1,048,576 bytes; 1000 CPU millis permits one CPU's scheduling capacity; a PID limit bounds processes/threads counted by the container. These Docker limits do not promise throughput or startup success.

**API / endpoint:** an interface another program calls. An endpoint is a particular operation, such as `POST /orders`.

**Artifact / bundle:** a saved output file, or the folder collecting a run's files. Artifacts include the plan, history, snapshots, and report.

**Atomic transaction:** a group of database changes committed together or rolled back together. It protects a purchase from being half-written, but does not automatically prevent a later transaction from repeating it.

**Authoritative state:** the source trusted to determine what actually happened. Here it is PostgreSQL's records, not an HTTP status or log message.

**Backoff:** time waited before retrying. The example waits 100 ms. This is an experiment setting, not a universal production retry recommendation.

**Baseline:** the recorded starting state, before the workload runs.

**Business rule / invariant:** a property that should hold, such as “one purchase has at most one debit.” Multiple rules are needed because correct arithmetic does not imply correct identity.

**Canonical representation:** a consistent way to serialize data so logically identical data gets the same bytes for hashing, such as sorting JSON object keys.

**CI (continuous integration):** automation that checks code changes. The same CLI can run in Linux automation, but the project's dedicated CI workflow remains deferred.

**Client:** the requester. It can stop waiting even while the server continues processing.

**Commit / rollback:** commit accepts a transaction's changes; rollback discards uncommitted changes. A lost response cannot tell the client which occurred.

**Concurrency:** several operations making progress during overlapping periods. The current scenario contract permits one attempt at a time.

**Container / image:** an image packages a program and its dependencies; a container is an instance created from that image. A stopped container can still exist and need cleanup.

**Coordinator / engine / harness:** the program organizing the experiment. A harness includes the test machinery around the application. A harness error is different from a business-rule violation.

**Correlation ID:** an identifier linking a response or event to its request. Faultline distinguishes run, logical operation, and individual attempt identities.

**Daemon:** a background service. Docker's daemon manages containers independently of the CLI process that requested them.

**Deadline / timeout:** a deadline is a latest permitted completion time; a timeout is the result of waiting too long. A timeout is about observation, not proof of transaction rollback.

**Cleanup reserve:** time within a run's total budget that ordinary experiment work cannot consume. Faultline also reserves a smaller part of cleanup for writing its report.

**Control-flow exception:** a signal in Python that changes what code runs next. `DeadlineExceeded` inherits from `BaseException` so ordinary network-error handlers do not swallow the coordinator's stop instruction and retry.

**Debit / ledger:** a debit takes money from this example's wallet. A ledger stores individual money movements so a balance can be independently checked.

**Digest / hash:** a fingerprint computed from bytes. A changed file normally produces a different SHA-256 digest. An unsigned hash inventory detects inconsistency, not the author's identity or a malicious rewrite of both file and inventory.

**Disposable environment:** test resources created for one experiment and removed afterward. Fresh fixtures avoid contamination between trials.

**Fault injection:** intentionally introducing a failure condition. Currently Faultline injects controlled response suppression; database proxy faults and process restart scenarios are still planned.

**Fault coverage:** whether the faults required by the recipe actually happened. Declaring a fault is not evidence it was applied.

**Fixture:** a known starting dataset. Our buyer, wallet, product price, and stock are fixed fixture data.

**`flock` / file descriptor:** `flock` asks the OS to lock an opened file. A descriptor is the program's handle to that open file. The kernel lock, not mere file existence, prevents simultaneous supported operations.

**Idempotency:** repeating the same logical request does not repeat its business effect. An idempotency key identifies retries; it must be checked against the payload and committed result.

**Integration test:** a test using the real cooperating components. Faultline's integration suite creates actual Docker containers and PostgreSQL transactions.

**Isolation:** either separating environments/networks, or controlling what database transactions can see of each other's work. The context tells you which meaning applies.

**JSON / NDJSON / YAML:** structured text formats. JSON represents data objects; NDJSON stores one JSON object per line; YAML is used for readable scenario configuration.

**Logical operation / attempt:** one intended business action versus one delivery attempt. One purchase may need two HTTP attempts and must still have only one debit.

**Manifest:** the run's inventory and metadata: identity, stage, owned resources, versions, and artifact hashes. Recovery can use an incomplete run's valid ownership manifest without pretending the experiment completed.

**Monotonic clock:** a clock that moves forward for measuring elapsed time, unaffected by wall-clock corrections. Timestamps from separate machines/process clock domains should not be casually compared.

**Nonce:** a unique per-run ownership value placed in both the manifest and Docker labels. It helps distinguish the resources of one run from another; it is not a secret or cryptographic authorization token.

**Observer:** the helper that reads database evidence. Its observation connection has read-only permissions. Setup is a separate write-capable role.

**Outbox:** a database table containing work intended for later delivery, here an order-confirmation event. An outbox row proves intent, not successful message delivery.

**Ownership:** evidence that Faultline created and recorded a resource for a particular run. Recovery requires both its full Docker ID in the manifest and matching live labels.

**Paise / minor unit:** one hundred paise equal one Indian rupee. Integer minor units make the example's money arithmetic exact.

**Preflight:** checks made before an experiment, such as platform support, Docker availability, and local images.

**Pure function:** a calculation driven by its inputs rather than network calls or changes to the outside world. The verifier is designed as a pure judgment over supplied evidence.

**Quiescence:** no supported work remains that could still change the state we are about to judge. In this milestone, graceful application exit plus absence of its database sessions establishes it.

**Read-only / least privilege:** give a component only the permissions it needs. An observer should not be able to change the business data it measures.

**Receipt:** a separate operational record of a cleanup attempt. It does not replace an experiment report or establish a business verdict.

**Replay / comparison / repetition / reduction:** strict replay reruns a saved plan with identical images, adapter code, engine, and platform, refusing any drift; comparison reruns a saved plan against an explicitly chosen variant and records every difference; repetition reruns current inputs with fresh fixtures; reduction searches for a smaller plan that still reproduces one selected signature. See [replay](replay.md) and [reduction](reduction.md).

**Violation signature:** an adapter's stable fingerprint of one failure: invariant, version, violation code, and logical business identity, without volatile row IDs or times. Reproduction counting targets one signature, not any FAIL.

**Group / denominator:** a group is the saved record of several trials, such as a replay. A denominator is what a count is divided by; Faultline reports matching failures over started trials and over conclusive trials, and says `not_available` when a denominator is zero.

**`REPEATABLE READ`:** PostgreSQL transaction isolation that gives the observation queries a consistent view during one snapshot transaction. It does not itself prove that no application writes will happen afterward; that requires quiescence.

**Schema / schema version:** the expected structure of a table or data document, and an identifier for that structure. Versions prevent old readers silently misinterpreting new data.

**Seed:** a starting value for a pseudo-random generator. It can reproduce generated inputs, not every timing decision of the operating system. The current explicit scenario has no generator.

**SIGINT / SIGTERM / SIGKILL:** Linux process signals. The first two can request orderly stopping; SIGKILL cannot be caught and prevents ordinary cleanup code from running.

**SIGALRM / ITIMER_REAL:** the signal and process timer used to interrupt an over-budget coordinator stage. Python handles the alarm on its main thread. This is not a hard real-time guarantee for uninterruptible operating-system or native-code work.

**Stage budget / total budget:** the allowed duration of one part of an experiment versus its overall allowed duration. A later stage can receive less than its configured maximum when the total work allowance is nearly exhausted.

**Snapshot:** a captured view of all required business records with completeness metadata. It must preserve duplicates rather than silently deduplicating them.

**Stale run:** for the current recovery command, a recorded run in an output root whose execution lock can be acquired. It is not an age threshold.

**Symlink / FIFO:** a symlink redirects a filesystem path; a FIFO is a named pipe that can wait for another process. Recovery refuses redirected ownership paths and requires regular manifest files.

**tmpfs:** a filesystem backed by memory. The database uses it for small disposable experiments; it is not persistent disk durability.

**Transaction row lock:** exclusive coordination over selected database rows while a transaction runs. The correct application locks an account before checking and changing its purchase state.

**UUID:** a widely used format for identifiers with an extremely low accidental collision probability. Each run and cleanup attempt gets one.

**Unit test:** a focused test of a small behavior, often with controlled inputs instead of real services. It complements, rather than replaces, integration coverage.

**Workload:** the operations sent to the application. Currently it is one logical purchase with explicitly allowed retries.

**WSL2:** Windows Subsystem for Linux version 2. It provides the Linux execution environment used for this project's Docker experiments on Windows.

**Delta debugging (ddmin):** a search that removes groups of inputs, keeps a removal if the failure persists, and splits into smaller groups when no group can be removed. Faultline's version also removes everything that depends on a deleted unit. See [reduction](reduction.md).

**Bearer token / URL fragment:** a bearer token is a secret that grants access to whoever presents it. The URL fragment is the part after `#`, which browsers never send to servers. Faultline puts the viewer's token there so it stays out of logs. See [the viewer guide](viewer.md).

**Origin / Host header / DNS rebinding:** the Origin header names the website that made a browser request, and the Host header names the server the request was meant for. DNS rebinding tricks a browser into sending a hostile site's requests to a local server; checking Host stops it.

**Content-Security-Policy (CSP):** a response header that tells the browser which scripts, styles, and connections a page may use. Faultline's policy allows only its own packaged files.
