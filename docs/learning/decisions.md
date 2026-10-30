# Why Faultline is built this way

These are implementation decisions, explained for learning. They complement the [release-level decisions](../../specs/11-decisions.md); a future requirement may change a choice. Each entry states what works now, why we chose it, alternatives, and the cost. L-001 through L-032 were documented on 2026-10-23; L-033 through L-036 on 2026-10-24.

## L-001: Prove one failure before building a platform

**Choice:** start with one purchase, one lost response, one retry, and one independent judge.

**ELI5:** before building a whole science lab, make sure one experiment can tell a broken toy from a working toy.

**Reason:** the hardest risk is a misleading verifier, not a missing dashboard. The unsafe and corrected backends give us a positive detection case and a case that should pass.

**Alternative and cost:** a broad scheduler or React UI would add more code before establishing that the tool measures the right thing. The narrow choice delays external application support and does not yet deliver the complete release. Evidence: the real scenario matrix in [test_integration.py](../../tests/test_integration.py).

## L-002: Use Python and keep modules in one package

**Choice:** Python 3.12/3.13, FastAPI, Pydantic, HTTPX, psycopg, and Typer; React/TypeScript remains planned for reports. Use `uv.lock` to fix dependency versions.

**ELI5:** keep the laboratory instructions in one organized notebook, with a page for each job.

**Reason:** these are mostly bounded input/output tasks, not microsecond packet processing. Separate modules make responsibilities visible without introducing independent deployment services. Pydantic checks contracts, HTTPX sends requests, psycopg talks to PostgreSQL, and Typer exposes commands.

**Alternative and cost:** .NET would also be viable but conflicts with the user's preference. Distributed queues, a plugin framework, and a database for the tool itself are unnecessary for one local experiment. Python package and container versions must still be kept in sync; a lockfile does not eliminate compatibility testing.

## L-003: Own every test resource

**Choice:** create disposable containers directly through the Docker API, with no existing endpoints or arbitrary Compose files.

**ELI5:** experiment only on the toys you put in your own test box.

**Reason:** the engine can record exact resource identities, limit privileges, prevent published AUT ports, and verify ownership before cleanup. The local Unix socket is required. Windows execution uses WSL2 so process signals and locks follow Linux semantics.

**Alternative and cost:** adopting an existing stack is easier for onboarding but makes accidental targeting and cleanup much harder to control. The initial laboratory used only trusted bundled images and synthetic data; L-028 extends application image selection while preserving the owned-resource boundary and constrained helpers. Docker administration remains powerful host access; this is not a malicious-code sandbox. See [environment.py](../../src/faultline/environment.py).

## L-004: Use real PostgreSQL and disposable storage

**Choice:** PostgreSQL holds business state; its data directory is tmpfs. Each run gets a fresh fixture.

**ELI5:** use a real calculator, but erase its scratch paper before the next experiment.

**Reason:** mocks cannot establish SQL locking, permissions, or transaction behavior. Fresh state prevents one trial's balance or orders from contaminating the next.

**Alternative and cost:** persistent volumes could support storage-crash experiments but would complicate cleanup and reset. tmpfs uses memory and disappears with the container; these tests do not establish durability through power loss or database restart. Fixture size is intentionally tiny.

## L-005: Protect identity inside the transaction

**Choice:** correct-A locks account and product rows, checks purchase/key identity, and commits every business effect in one transaction. Unique indexes enforce purchase and retry identities.

**ELI5:** first check whether this is the same shopping request, then write the entire purchase on one page that is accepted or discarded as a whole.

**Reason:** a retry can arrive after a successful commit. Checking only client success is insufficient, and placing the idempotency record in a separate commit could leave the record and money movement inconsistent.

**Alternative and cost:** serializable transactions with retries could be a second independent implementation later. Account-row locking is straightforward but serializes purchases for one buyer. We have not measured high-concurrency throughput. Money uses integer paise to avoid floating-point rounding. See `transact_order` in [orders.py](../../src/faultline/orders.py).

## L-006: Suppress a complete response deliberately

**Choice:** record a real complete successful response, then hide it from the client retry policy.

**ELI5:** the shop wrote the purchase in its notebook, but we hide the receipt before the customer sees it.

**Reason:** this precisely exercises ambiguous success after commit. Guessing when to break a TCP connection would make the first experiment much less reliable.

**Alternative and cost:** Toxiproxy network faults remain planned. The current fault is honestly labeled client response suppression; it does not prove behavior under arbitrary packet loss. The required-fault coverage check prevents a different timeout from being counted as this experiment.

## L-007: Start the worker before timing requests

**Choice:** use one persistent helper process and a correlated JSON exchange over Docker exec.

**ELI5:** put the messenger on duty before starting the delivery stopwatch.

**Reason:** repeated acceptance exposed Python helper startup consuming the one-second attempt deadline. Moving startup outside workload dispatch made the intended response fault repeatable.

**Alternative and cost:** starting a process per attempt was simpler but distorted request timing. A persistent stream needs explicit framing, correlation, bounds, and careful close ordering. A later warning showed that closing a raw socket before its owning HTTP response was unsafe; the close-order regression test uses a real socket pair. See [worker.py](../../src/faultline/worker.py) and [test_worker.py](../../tests/test_worker.py).

## L-008: Judge stored facts independently

**Choice:** verify complete database snapshots and attempt evidence with a pure verifier; do not inspect the backend variant name to determine the verdict.

**ELI5:** the teacher checks the working, not the student's name or their claim that they finished.

**Reason:** HTTP success alone cannot prove exactly one debit. Missing rows, duplicate rows, or a false success response must be visible. Independent hand-written verifier fixtures avoid generating expected answers using the same transaction code being tested.

**Alternative and cost:** application assertions or log searches would be easier, but could share the application's mistake. The snapshot projection is still part of the trusted observation path and needs tests. Only declared invariants are checked. See [verifier.py](../../src/faultline/verifier.py).

## L-009: Establish quiescence by graceful shutdown

**Choice:** stop the synchronous application gracefully and check for zero application database sessions before accepting final state.

**ELI5:** wait until everyone has put down their pencils before counting the marks on the page.

**Reason:** the client can time out while the server is still changing data. Repeatedly seeing the same balance is not proof that no later write will occur.

**Alternative and cost:** the release's admission/in-flight registry will support richer service lifecycles. It is not implemented yet. Stopping the process is simpler for the current fixture but cannot test resumed writers or background jobs. Forced termination makes final-state certainty unavailable.

## L-010: Separate a broken application from a broken test

**Choice:** distinguish FAIL, INCONCLUSIVE, and ERROR; record fault coverage and retain findings even when ERROR dominates.

**ELI5:** “the answer is wrong,” “I cannot see the answer,” and “my measuring tool broke” are three different outcomes.

**Reason:** a harness crash must not count as successful defect detection, and a missed required fault must not turn into PASS. Completion is also required: doing nothing is not a correct purchase implementation.

**Alternative and cost:** one red/green status would be easier to display but less truthful. Consumers and CI must handle distinct exit codes. A client timeout can coexist with successful business completion because the database committed independently of client knowledge.

## L-011: Keep evidence in ordinary files

**Choice:** JSON plans/reports/snapshots, NDJSON history, Markdown summaries, and content hashes; no database for Faultline's own results.

**ELI5:** keep the recipe, notebook, and final measurements together in a labeled folder.

**Reason:** bundles can be inspected offline and retained after containers are deleted. Hashes expose accidental changes relative to the inventory. Atomic replacement reduces half-written JSON documents.

**Alternative and cost:** a database-backed catalog might later help search many runs. Today it would add a second storage system without improving the core experiment. Hashes are not authenticity signatures. File sizes are bounded, and full-disk failures can still leave incomplete evidence. See [artifacts.py](../../src/faultline/artifacts.py).

## L-012: Use an operating-system lock for recovery

**Choice:** `run_one` and cleanup acquire the same nonblocking `flock` on `execution.lock` in the output root.

**ELI5:** only the person holding the laboratory key may run an experiment or pack it away.

**Reason:** checking whether a PID exists is weaker: PIDs can be reused, and a “check, then delete” sequence can race a new run. Holding the lock through work closes that gap for supported callers. A hard-killed process releases the kernel lock automatically.

**Alternative and cost:** a PID file, timestamp threshold, or container status is not sufficient liveness evidence. The root-wide lock is conservative: it blocks cleanup of one stale run while another run in that root is active. Different roots remain independent. Each repeated trial acquires its own lock, so another command can enter between trials. This is not a workspace-wide or distributed lock.

**Evidence:** unit tests cover contention through direct engine calls and CLI cleanup; the hard-kill integration test exercises real child processes. Low-level `Environment` helpers are internal building blocks, not a public unlocked execution interface.

**Review correction:** a copied bundle can contain genuine resource IDs but sit beside a different lock. The manifest therefore records `execution_root`, and recovery requires the current resolved root to match. Copied/moved bundles and legacy manifests without this field are refused for automatic cleanup. Offline report reading stays portable. This sacrifices recovery of old or relocated bundles rather than pretending their original run is known to be idle; never edit the field to bypass the check.

## L-013: Validate ownership records before touching Docker

**Choice:** `clean --stale` reads at most 1,000 entries under the selected `runs/`, validates all selected manifests first, then cleans only their recorded IDs after checking current labels.

**ELI5:** check your packing list and the label on each box before taking anything away.

**Reason:** names, short IDs, or loose label searches can select the wrong resource. Recovery accepts a known bundle version/profile, canonical UUIDs, full 64-character Docker IDs, allowed roles/kinds, and no duplicate identities or roles. A manifest is capped at 1 MiB and six resources for this profile. Symlinked descendants, nonregular manifest files, and duplicate JSON fields are refused.

**Alternative and cost:** sweeping all `fl-*` resources would find more leftovers but weaken ownership. One malformed member stops a stale batch before mutation; `--run` can select another valid bundle. Completed runs are checked too, because “completed” does not guarantee cleanup succeeded. No age threshold is used: non-live means the output lock is available. Unknown profiles need a future versioned recovery contract.

## L-014: Record cleanup separately from experiment results

**Choice:** write a new receipt in `<output>/cleanup/` for each recovered run. Write an initial incomplete receipt before deletion and a final result afterward. Never rewrite original plans, history, snapshots, or reports.

**ELI5:** keep the experiment notebook unchanged and add a separate note saying when you cleaned the table.

**Reason:** cleanup cannot retroactively establish what the application did. A hard-killed experiment remains incomplete. A failed receipt update leaves conservative evidence that can be followed by another cleanup attempt.

**Alternative and cost:** editing `report.json` would change historical evidence and require rebuilding its integrity inventory. Separate receipts preserve history but require readers to distinguish original cleanup status from a later receipt. Receipts are versioned operational records, not signed proof and not a new business verdict. If the initial receipt cannot be written, deletion does not start.

## L-015: Prefer refusal to guessed recovery

**Choice:** missing/mismatched ownership means refusal, even after a crash. Already-absent resources count as successfully cleaned.

**ELI5:** if you lose the packing list, do not guess which boxes belong to you.

**Reason:** the critical safety property is avoiding unrelated-resource deletion. Repeating cleanup must be safe because a previous attempt may have stopped halfway through.

**Alternative and cost:** the original implementation could not recover a resource created before its ID was saved. L-033 through L-035 now add prewritten creation intents and durable reconciliation for new runs. Missing legacy intents and unresolved absent creations remain conservative refusals. Per-request Docker timeouts exist, but a whole recovery-command deadline and guaranteed machine-crash durability are still deferred.

## L-016: Keep the outbox claim narrow

**Choice:** save an order-confirmed intent in the same database transaction; introduce no broker yet.

**ELI5:** write “send the receipt” on a durable to-do list. That is not proof the receipt was delivered.

**Reason:** this tests atomic creation of the intent without inventing delivery guarantees. Message brokers, consumers, duplicate delivery, and out-of-order messages need separate scenarios and evidence.

**Alternative and cost:** adding Kafka or another broker now would broaden the experiment before its delivery contract exists. Faultline currently proves neither exactly-once messaging nor eventual delivery.

## L-017: Separate repetition from replay

**Choice:** `--repeat` reruns the current validated scenario against fresh fixtures and records counts.

**ELI5:** repeat the recipe with new ingredients; do not claim the universe moved in exactly the same way each time.

**Reason:** fixed inputs do not control operating-system or database scheduling. Historical compatibility checks, deterministic scheduling, and failure reduction are different features.

**Alternative and cost:** naming repetition “replay” would overstate the guarantee. Measured frequencies include denominators, code phases, and coverage. Historical acceptance results are preserved separately from current tests.

## L-018: Document the implementation as a learning project

**Choice:** keep plain-language walkthroughs, a glossary, decision records, source links, and test evidence alongside code.

**ELI5:** explain the experiment well enough that someone else can perform it and say why it works.

**Reason:** the user explicitly requested learning explanations for every meaningful decision. Repository instructions make this an ongoing requirement.

**Alternative and cost:** comments alone cannot explain cross-module behavior; a giant line-by-line narrative would quickly become stale. These guides explain contracts and reasons, while local comments cover subtle code. Updating behavior now requires updating the corresponding explanations and tests.

## L-019: Reserve cleanup within one absolute run budget

**Choice:** enforce stage maxima and an absolute total-work boundary before the cleanup reserve. Initialization shares provisioning's original deadline; new stages cannot reset the total clock. The run budget starts after preflight and lock acquisition.

**ELI5:** stop the experiment early enough to put the equipment away before the rental period ends.

**Reason:** several individually bounded operations can still take too long in aggregate. Cleanup needs its own guaranteed opportunity to run, even when setup or execution fails.

**Alternative and cost:** independent per-call timeouts alone cannot bound a sequence of calls. Stage maxima do not all fit into every total budget; the remaining total work allowance clips later stages. Existing shorter helper timeouts still apply. Defaults now follow the specification, including a 60-second execution limit instead of the initial implementation's 30 seconds. Effective plan hashes change when omitted defaults change.

## L-020: Use a main-thread alarm for this trusted Linux coordinator

**Choice:** use `SIGALRM`/`ITIMER_REAL`, restore handlers after each stage, reject active caller timers and non-main-thread execution, and raise a `BaseException` subclass for deadline control flow.

**ELI5:** an alarm must interrupt the supervisor even while they are waiting; it must not be mistaken for a customer's lost reply.

**Reason:** clock checks between operations cannot interrupt a blocked operation. A generic `TimeoutError` could be caught as a transport failure and retried. A dedicated control-flow exception reaches the engine's cleanup path.

**Alternative and cost:** timed-out threads can keep running. A supervisor process offers stronger separation but needs additional lifecycle, ownership, and evidence protocols. This implementation is deliberately Linux/main-thread-only and is not hard real-time protection against uninterruptible kernel calls or uncooperative native code. The [deadline guide](deadlines.md) explains those limits.

## L-021: Preserve facts when work times out

**Choice:** keep completed attempts and fault decisions in shared per-run accumulators. Execution expiry stops admission but permits bounded drain/observation where time remains. Drain expiry removes certainty; verification or observation expiry is a harness error.

**ELI5:** if the stopwatch ends halfway through the experiment, keep the notes already written and say what you did not finish.

**Reason:** a timeout does not establish rollback. Losing all previous attempt evidence because a function never returned would make the report misleading. Conversely, inventing a terminal client response or a completed workload would also be wrong.

**Alternative and cost:** aborting every deadline directly to a generic ERROR loses useful application evidence; treating every timeout as a business FAIL confuses client knowledge with committed state. A total-work timeout can still prevent final observation, so not every interrupted run gets a useful verdict.

## L-022: Reserve report-writing time inside cleanup

**Choice:** reserve the smaller of one second or one quarter of `cleanup_ms` for finalization. Preserve unresolved resource IDs as cleanup progresses; retain verified findings when cleanup fails. Finalization remains inside cleanup and total limits.

**ELI5:** leave a little packing time to write down which equipment is still on the table.

**Reason:** using every cleanup millisecond on deletion would leave no chance to explain failure or support later scoped recovery. A worker stream-close exception must not prevent attempts to remove its container.

**Alternative and cost:** unbounded emergency reporting would violate the total budget. A very small budget, full disk, or delayed signal can therefore leave only a partial bundle. Timing in a report ends before report assembly; we do not fabricate finalization duration before it has finished. Standalone recovery command deadlines remain future work. L-033 through L-035 subsequently add creation-record recovery without changing these timer guarantees.

## L-023: Introduce a checked internal adapter before external registration

**Later extension:** L-030 now permits installed packages implementing this same order bridge. The built-in continues to advertise `external_registration: false`; selected installed adapters advertise true. The original sequencing rationale below remains useful, and full public API acceptance remains pending.

**Choice:** route domain preparation, readiness, quiescence, snapshots, and verification through a built-in adapter; keep Docker ownership, scheduling, budgets, and cleanup in the engine/environment. Expose its capabilities during validation. Move schema setup out of infrastructure provisioning.

**ELI5:** give the lab supervisor an assistant who understands the shop, while the supervisor keeps the room keys and stopwatch.

**Reason:** the engine should not import the order judge directly. A tested internal boundary lets us separate responsibilities before accepting externally installed integrations.

**Alternative and cost:** loading arbitrary installed adapters immediately would expose an API while the scenario parser, worker, and topology are still order-specific. The bridge therefore has an explicitly internal boundary version and advertises external registration as false. It is an M2 increment, not completion of public-adapter acceptance.

## L-024: Check the completeness and consistency of grading results

**Choice:** require exactly one result for each requested invariant, its declared version and logical identity, consistent status/failure/signature fields, and evidence references. Validate snapshot envelopes and bounded JSON returns too. Ordinary hook exceptions and invalid returns produce structured ERROR diagnostics.

**ELI5:** a teacher cannot mark a seven-question exam complete after grading only six answers.

**Reason:** a plausible-looking PASS list can hide a broken verifier. Application FAIL and harness ERROR must remain different conclusions. Diagnostics identify the failed hook without echoing arbitrary exception text or returned payloads.

**Alternative and cost:** trusting every hook result is simpler but can produce false success. Contracts add validation code and cannot establish that a well-formed judge is correct, so hand-audited verifier tests and real PostgreSQL experiments remain required. Canonical JSON detaches return values but is not a memory or execution sandbox.

## L-025: Record source provenance separately from scenario intent

**Later extension:** L-031 adds installed distribution files and factory identity alongside the bundled source inventory; the original bundled hash format remains supported.

**Choice:** save an adapter artifact with capabilities, package version, and a bounded inventory of exact package Python-source hashes. Reference it from the manifest, history, and report. Preserve existing plan semantics and offline reading of older completed bundles.

**ELI5:** keep the recipe and write down which instruction book the grader used beside it.

**Reason:** a version string alone does not identify an edited local verifier. Hashing the shared bundled package avoids guessing which local imports can affect its behavior.

**Alternative and cost:** hashing only the adapter file misses verifier changes; hashing the entire machine is expensive and misleading. The selected scope can change for unrelated CLI edits and does not attest dependencies, in-memory monkeypatches, or container equivalence. Strict replay will need additional compatibility checks. See the [adapter guide](adapters.md) for exact scope and limits.

## L-026: Make the project subset executable and reject unsupported settings

**Later extension:** L-030 implements installed order-adapter names and removes only that rejection. The remaining topology, environment, routing, and readiness restrictions still apply.

**Choice:** support a four-service order manifest with fixed role/route/readiness bindings, known variants, configurable bounded resources, and project ceilings. Reject proxy topology, arbitrary environment values, added writable paths, readiness overrides, and external adapter names until their behavior is implemented.

**ELI5:** accept only equipment the laboratory actually knows how to operate.

**Reason:** accepting configuration without applying it gives users false confidence. The first project increment should run end to end, with live-container tests demonstrating that accepted settings take effect.

**Alternative and cost:** a parser for the entire target schema would be easier to advertise but would accept settings the runtime cannot honor. This subset deliberately differs from the complete public schema's five-service layout and default-variant behavior; unsupported features fail clearly. The target specifications are unchanged.

## L-027: Distinguish ceilings from requested values

**Choice:** clip omitted scenario defaults to project ceilings; reject explicit over-ceiling values; then validate the full effective scenario again. Save actual run values in the plan. Omitted project ceilings mean engine hard caps.

**ELI5:** if you did not choose a cooking time, the kitchen may supply a shorter default. If you explicitly ask for more time than the booking allows, it should tell you instead of quietly changing the experiment.

**Reason:** merging configuration is part of experiment semantics. Changing an explicit timeout can change whether a retry occurs. Total/cleanup constraints and retry budgets must still hold after defaults are clipped.

**Alternative and cost:** always taking the minimum hides conflicting intent; always rejecting default values makes a tighter project ceiling unnecessarily hard to use. Explicit-field tracking and post-merge validation add a little code but produce explainable behavior.

## L-028: Pin selected local images without changing the network boundary

**Choice:** allow tagged/digest/full-ID local application references in declared variants. Keep database/helper references constrained to the bundled profile, resolve selected images before execution, check Linux x86-64 architecture and aggregate host capacity, and pass immutable IDs to Docker.

**ELI5:** choose a sealed box already in the laboratory and write down its exact identity before starting.

**Reason:** projects need to select application builds while retaining controlled dependencies. Looking up a local image must not become an implicit registry pull or an arbitrary endpoint option.

**Alternative and cost:** keeping a single application tag prevents useful build comparisons; accepting arbitrary service graphs would require new ownership, credentials, routing, and quiescence contracts. Custom application images remain trusted and must satisfy the existing order schema, permissions, port, and graceful-exit profile. Independent external integration is still a future acceptance gate.

## L-029: Keep recovery independent of current configuration

**Choice:** record project identity and its configuration artifact before resource creation. Use the recorded project label during recovery, alongside exact IDs and existing run/nonce labels. Report/cleanup do not load project YAML. Discover a default project only in the current directory for validate/run/doctor; never silently ignore an invalid one.

**ELI5:** equipment tags and the checkout receipt decide what belongs to yesterday's experiment, even if today's recipe changed.

**Reason:** editing, moving, or deleting a project must not redirect cleanup or prevent offline inspection. Scenario paths are confined to the selected project root and loaded through bounded regular-file reads.

**Alternative and cost:** reloading YAML during cleanup couples recovery to mutable inputs. Always requiring a new project file would break the existing demo unnecessarily, so absence of a default file preserves legacy implicit runs. The current path checks do not sandbox hostile concurrent filesystem mutation; the original execution-root lock and ownership restrictions remain required.

## L-030: Select trusted installed adapters without expanding the order contract

**Choice:** accept a simple project entry-point name, discover it in the installed `faultline.adapters` group, require one unambiguous registration and an exact distribution version, then construct a fresh checked adapter. Keep `bundled_orders` reserved. Require all lifecycle hooks and matching description identity before Docker. YAML parsing itself remains inert.

**ELI5:** the recipe names an assistant already in the building. The supervisor checks their address card and qualifications before booking equipment.

**Reason:** package selection is a concrete next step toward reusable tooling. It can be implemented and exercised without pretending the order-specific parser, routing, helpers, and schema already support arbitrary applications.

**Alternative and cost:** arbitrary `module:object` paths in YAML blur configuration and code loading; automatic installation adds dependency/network behavior to an experiment. Full service/domain extensibility now would require unimplemented scheduler and quiescence contracts. Installed adapters remain trusted Python code, not sandboxed plugins. The separate example reuses the existing verifier and is explicitly not independent correctness validation. Details and installation commands are in the [guide](installed-adapters.md).

## L-031: Fingerprint actual installed files and the shared judge

**Choice:** parse a bounded wheel RECORD, read actual regular files inside the installation root, and hash their bytes. Combine that inventory with the factory address and Faultline's source hash. Refuse missing files, traversal, symlinks, editable/native installations, and an imported entry module at another source path. Generated bytecode is excluded.

**ELI5:** record the pages actually delivered, including the rulebook the assistant shares with the supervisor, rather than trusting the edition number on the cover.

**Reason:** a wrapper adapter may delegate all grading to Faultline; fingerprinting only the wrapper misses changes to the real judge. Python 3.13's convenience inventory silently removes missing paths, so using it would turn an incomplete installation into apparently complete provenance. Tests exposed this and now protect the direct-RECORD check. Discovery also inspects distributions individually because the convenience entry-point API deduplicates installed copies by name.

**Alternative and cost:** trusting package version or declared RECORD hashes is simpler but does not describe actual bytes. Hashing every installed dependency would be broader, more expensive, and still not attest to running memory. The current inventory has explicit file/byte/count limits and records installer metadata, so equivalent installations can differ. Import caches, dependencies, unlisted files, hostile code, and concurrent edits remain outside the guarantee. This evidence is not signature verification, a sandbox, container equivalence, or replay compatibility enforcement.

## L-032: Give adapter preflight its own bounded allowance

**Choice:** allow ten seconds per adapter resolution for discovery, hashing, import, construction, description, and validation. Reuse the Linux main-thread alarm mechanism. CLI run checks before Docker preflight; direct `run_one` also checks, creating a fresh adapter per repetition. Keep runtime hooks in their existing stage budgets. Report and recovery never resolve packages.

**ELI5:** checking the assistant's credentials gets its own short appointment. Inspecting yesterday's notebook does not require rehiring that assistant.

**Reason:** an installed module can hang even before the first hook. Starting the run timer only after Docker preflight would leave that work unbounded. Separating selection from evidence reading also means uninstalling a package does not prevent investigation or cleanup.

**Alternative and cost:** a subprocess sandbox would need a larger serialization, capability, cancellation, and environment-control protocol. It is unnecessary for the current trusted-code contract. The timer is not hard real-time enforcement and cannot safely police hostile code. Duplicate validation adds bounded overhead and requires side-effect-free hooks; total scenario time excludes adapter preflight. Preflight failures have structured CLI diagnostics but no run bundle because no experiment has started.

## L-033: Write creation intent before contacting Docker

**Choice:** save exact generated name, kind/role, and a fresh UUID token in the manifest before each create request. Apply that token and role/kind as resource labels alongside existing run/project/nonce labels. Require one intent per supported role. Reconciliation may inspect only the exact recorded name and must verify every label before binding a full immutable ID.

**ELI5:** write down a numbered claim ticket before ordering equipment, so a lost delivery receipt does not force us to guess which equipment is ours.

**Reason:** Docker can create a resource even when the coordinator dies before saving its ID. The daemon and local filesystem cannot participate in one atomic transaction. Prewritten intent provides an explicit, narrow ownership record for this gap.

**Alternative and cost:** loose label/prefix scans can mistake unrelated resources for ours; requiring an already recorded ID leaves the creation window unrecoverable. The intent adds a small synchronous write and three labels per resource. It does not authorize mutation until an ID has also been persisted and rechecked. Trusted local Docker and workspace assumptions remain; this does not defend against a malicious administrator copying ownership labels.

## L-034: Persist recovered IDs outside original experiment evidence

**Choice:** live cleanup saves reconciled IDs in its still-active manifest. Later `clean` saves bindings under `cleanup/bindings/<run-uuid>.json`, tied to the run/nonce and exact original-manifest hash. Validate all selected bindings before deletion; write each new binding before granting mutation rights. Reuse bound IDs on subsequent cleanup attempts.

**ELI5:** put the recovered serial number in a separate recovery notebook. Never rewrite yesterday's experiment, and never swap the serial number for another box just because its name was reused.

**Reason:** another crash can occur between binding, deletion, and the final cleanup receipt. Durable bindings make retries safe while preserving the original artifacts. An interrupted receipt does not erase the ownership information already saved.

**Alternative and cost:** rewriting an original manifest breaks evidence preservation. Saving IDs only in a final receipt leaves another missing-record window. This introduces one small recovery-state file per reconciled run, bounded by the existing six-role inventory. Users must retain it with their manifests and receipts. It is not a separate database, automatic repair mechanism, or power-loss durability guarantee.

## L-035: Treat an absent pending creation as uncertain

**Choice:** a missing bound ID counts as successfully absent; a missing unbound intent remains in `remaining_resources` and makes cleanup incomplete. Wrong ownership, ambiguous lookup, or inability to persist a binding also leaves the intent unresolved. A later cleanup may recover a delayed object if it appears.

**ELI5:** an empty delivery doorstep does not prove the warehouse stopped making your box.

**Reason:** a failed or interrupted client request does not prove the daemon operation was cancelled. Treating a temporary absence as success could hide a later resource leak.

**Alternative and cost:** automatic retries could create duplicates; declaring absence final would be an unsupported guarantee. A crash after saving intent but before submitting creation may leave permanent uncertainty despite no actual leftover. An explicit abandonment policy or reliable daemon-side request completion protocol is deferred. The tool reports uncertainty rather than inventing success. The [creation recovery guide](creation-recovery.md) maps these decisions to code and tests.

## L-036: Reconcile an interrupted event append before recording another event

**Choice:** if an event append raises an exception or signal, compare the bytes at that append's original offset with the exact intended event. A complete matching event advances the sequence once; no bytes means no advancement; partial or unreadable data prevents further append and completed-report finalization. Preserve all original bytes and re-raise the interruption.

**ELI5:** if you were interrupted after writing line four but before moving your bookmark, check the notebook before writing another line four.

**Reason:** the real SIGTERM regression exposed a complete `ready` event followed by a cleanup event with the same sequence number. File persistence and the in-memory counter are not one atomic operation. The resulting report correctly failed offline integrity validation even though container cleanup succeeded.

**Alternative and cost:** blocking cancellation signals through `fsync` would delay timer handling; advancing the counter before writing can leave a gap; deleting a partial line would discard evidence. The chosen repair reads at most the attempted event length plus one byte on an exceptional path and does not reconstruct missing contents. A genuinely partial write now leaves an explicitly incomplete bundle rather than a completed but corrupt report. This handles ordinary process interruption, not power-loss durability. Tests in [test_artifacts.py](../../tests/test_artifacts.py) interrupt after a real `fsync` and inject a partial write.

## L-037: Materialize values before concurrent admission

**Choice:** use versioned SHA-256 counter expansion for the bundled purchase generator and save every body, identity, default, and potential attempt. Bind the selected adapter's fixture digest and property versions before execution.

**ELI5:** write the entire recipe before several cooks begin; the fastest cook must not choose the next ingredient for everyone else.

**Reason:** asynchronous completion must not affect generated values. **Alternative and cost:** a shared random generator is simpler but scheduling-dependent. Counter hashing is stable but modulo mapping is not a claim of unbiased statistical sampling. Saved plans support later replay work; they are not deterministic execution.

## L-038: Keep scheduler decisions and evidence writes on one coordinator

**Choice:** bounded transport threads return results to one scheduler. It owns dependency/barrier state, conditional retry admission, fault gates, and history writes. Backoff releases capacity; response suppression retains it.

**ELI5:** one teacher keeps the attendance register while students work independently.

**Reason:** competing threads must not write conflicting event sequences or exceed concurrency. **Alternative and cost:** an entirely asynchronous coordinator would require converting blocking Docker and trusted adapter calls; independent per-request processes waste memory. Current controller calls can delay coordinator processing, and actual completion order remains nondeterministic. Tests prove bounded overlap and admission rules rather than assuming a barrier creates overlapping database transactions.

## L-039: Multiplex a bounded acknowledged worker stream

**Choice:** protocol version 2 uses one reader, serialized writes, per-command mailboxes, positive admission acknowledgment, and correlated results. The worker shares one Python interpreter across at most 32 HTTP tasks.

**ELI5:** numbered tickets let several orders finish in any order without handing one customer's receipt to another.

**Reason:** an interpreter per slot exceeds the small-container budget. **Alternative and cost:** sequential transport cannot exercise concurrency; unbounded tasks can exhaust memory. EOF, unknown IDs, missing acknowledgments, and helper deadlines are harness errors. No implicit command retry is attempted. A tested reversed-response exchange protects correlation.

## L-040: Separate data traffic, administration, and independent observation

**Choice:** four internal networks and five owned containers; bind proxy administration to a control-only DNS alias. Pin Toxiproxy 2.12.0 and require configuration read-back for application and healing. Extend recovery's strict ownership inventory to nine roles.

**ELI5:** put the equipment controls in a room the tested application cannot enter, and give the examiner a separate window onto the database.

**Reason:** bypassing the proxy invalidates a fault experiment; exposing admin permits the application to alter its test. **Alternative and cost:** a static endpoint address was rejected on Docker's automatically allocated subnet. A control-only alias avoids manual subnet management. The wildcard data listener's normalized address is explicitly checked. Real probes test forbidden connections, existing-connection termination, and new-connection refusal. Configured latency does not identify a SQL statement. One fault per binding avoids inventing ambiguous overlap semantics.

## L-041: Register work before mutation, reconcile only after process and session exit

**Choice:** commit an in-flight row with process epoch and database PID/start time before business work. Retain uncertain rows after connection errors. Drain confirms owned-process exit and zero application sessions before clearing stale diagnostic registrations.

**ELI5:** sign into the workshop before touching anything; an empty chair is not proof the job finished.

**Reason:** client timeout and process restart can leave work or stale registrations. **Alternative and cost:** repeated equal balances or handler response flags cannot prove completion. The extra connection and registration write add overhead and are test instrumentation. The bundled profile has no background mutation after handler exit; other applications must supply their own completion contract. Forced drain remains uncertain.

## L-042: Generalize the boundary using an independent counter application

**Choice:** accept declared local role images, internal routes, generic integer JSON requests, trusted helper modules, and pure adapter findings. Demonstrate a separately built counter wheel/image with independent tables and verification. Public quiescence reports ready/pending/unknown with evidence.

**ELI5:** test whether the examination system can judge a different subject, rather than renaming the original textbook.

**Reason:** inheriting OrdersAdapter only proves packaging. **Alternative and cost:** arbitrary service graphs, database engines, or hostile-code sandboxing are larger problems and remain outside v1. The five-role PostgreSQL topology, compatible helper protocols, fixed security settings, and explicit image runtime contracts still apply. Secret references are resolved locally and never persisted as resolved values. This counter example does not replace M4's transfer-domain gate.

## L-043: Reconcile per-intent effects against whole-state totals

**Choice:** emit every invariant for every logical identity; scope atomicity/idempotency to the relevant intent while summing all ledger and inventory records. Report unrequested effects and orphan tokens explicitly. Validate expected rejection responses and absence of new effects.

**ELI5:** check each receipt, then reconcile the entire cash drawer; hiding an unknown receipt must not make the arithmetic pass.

**Reason:** the one-purchase verifier's global count would falsely fail legitimate independent purchases; naive filtering would hide orphan effects. **Alternative and cost:** checking HTTP status alone misses committed duplicates and ambiguous timeouts. The bundled verifier still has a deliberately small fixture and seven specific rules. Retryable database 503s are transport evidence, not proof that a transaction rolled back.

## L-044: Check registered generator output as ordinary untrusted input

**Choice:** add an optional trusted adapter `expand(generator, seed)` hook with an exact output contract. Validate the returned operation count/version and every request, then rerun graph and budget checks on the complete materialized workload. Save both values and algorithm metadata. Keep the bundled purchase expansion available as version 1.

**ELI5:** a recipe writer may hand us instructions, but we still count the ingredients and check that step two does not wait forever for step three.

**Reason:** hardcoding only the purchase generator would prevent a separately packaged domain from using the same experiment engine. Parsing a custom generator happens before its adapter is loaded, so expanded dependencies and attempt totals can only be checked after the hook returns. Tests demonstrate that this second check cannot bypass project ceilings. The independent counter example supplies its own generator and verifies actual resulting rows.

**Alternatives and tradeoffs:** loading arbitrary Python from YAML would violate the installed-code boundary. Implementing every domain generator in the core would couple the engine to applications. Trusted hooks keep domain logic in the adapter, but their determinism is the author's responsibility; three-process checks provide evidence for the bundled examples, not proof for every third-party plugin. Each expansion is bounded to 1 MiB and preflight has a ten-second total allowance. Invalid output never reaches Docker. Exact replay and compatibility checking remain M3.

## L-045: Keep validation phases and claims separate

**Choice:** preserve the full 38-case regression result, the final focused runtime checks, and earlier failed provisioning experiments as distinct measurements. Reports now describe the recorded workload and identify the logical intent beside each finding.

**ELI5:** a school report should say which exam was taken on which day; combining marks from different exams must not look like one perfect exam.

**Reason:** the final generator contract changed after the full regression run. A focused real follow-up covers those changes without pretending the earlier run tested later source. The disk-full test injects an ENOSPC exception at the artifact boundary; it does not exhaust the user's physical disk.

**Alternative and limit:** repeating every long Docker test after documentation or isolated contract changes adds cost without resolving new uncertainty. Focused checks supplement, rather than rename, the existing full pass. M2 completion does not establish deterministic execution, release-wide defect discrimination, CI performance, or M3/M4 functionality. The acceptance report links each claim to its actual evidence.

## L-046: Make strict replay refuse rather than adapt

**Choice:** rebuild the scenario from the saved `plan.json` and saved project configuration, then require an identical plan hash, engine version, adapter package/version/content hash, and complete platform record (OS, architecture, kernel, Docker version, every image ID, project hash, and variant). Any difference returns exit code 2 with the differing fields before resources are created.

**ELI5:** repeating an experiment means using the same recipe, oven, and ingredients. If the oven was replaced, say so instead of calling it a repeat.

**Reason:** a replay that silently uses a rebuilt image or edited verifier could "reproduce" or "fix" a failure for an unrelated reason. **Alternatives and tradeoffs:** tolerating minor version drift would make old evidence replayable longer, but nobody could tell which differences mattered. Re-reading the current project YAML would be convenient, but the file may have changed since the run. Because the bundled adapter's content hash covers all engine source, any source edit makes old bundles non-replayable; comparison is the supported route after upgrades. Replay still does not reproduce operating-system or transaction scheduling.

## L-047: Separate comparison from replay and reject meaningless comparisons

**Choice:** `compare` binds the saved plan to an explicitly named variant under the current project, records every environment and identity difference in the group and each new manifest, and rejects comparisons whose requests, invariant versions, or fixture identity differ.

**ELI5:** comparing two flours is useful; comparing a cake with a pie is not.

**Reason:** a before/after claim is only meaningful when the business inputs and the definition of correctness stay the same. **Alternative and cost:** allowing any comparison and printing a warning would let a different invariant version make a fix look successful. The current check cannot tell whether two different images are semantically equivalent; it reports the difference and leaves interpretation to the reader.

## L-048: Count reproductions conservatively and persist groups per trial

**Choice:** a reproduction is a completed FAIL that contains the target signature. ERROR and interrupted runs never count, even when they contain the violation. Report matching failures over started trials and over conclusive trials, with `not_available` for zero denominators. Rewrite the group record after every trial.

**ELI5:** if the thermometer broke during an experiment, you cannot count that reading, even if it happened to show the number you expected.

**Reason:** a harness error means the evidence chain is broken, so counting it would overstate reliability. Two denominators show both "how often did it happen per attempt" and "how often among trustworthy attempts" without hiding either. **Alternatives and tradeoffs:** a single success rate is easier to read but can hide errors; a probability estimate would claim more than a handful of trials supports. Group files are plain JSON and are not themselves hash-inventoried; they reference hash-verified run bundles.

## L-049: Let adapters declare additional selectable fixtures

**Choice:** add an optional `fixtures` map (name → version and hash) to the adapter description. The default `fixture` keeps its existing two-argument `prepare` call; only a declared additional fixture is passed as `prepare(environment, variant, fixture=name)`. The selected identity is bound into the plan, and baselines and snapshots are checked against it. The bundled adapter adds `reduction_market`. The verifier keeps its own expected opening rows and reconciles every fixture account and product separately.

**ELI5:** a teacher can hand out a different worksheet, but must write its name on the answer sheet, and the marker keeps their own answer key.

**Reason:** AT-016 needs real, independent noise purchases that do not share the target buyer's wallet. **Alternatives and tradeoffs:** changing every adapter's `prepare` signature would break external packages; encoding the fixture in the variant would mix application choice with data choice. The application records the seeded fixture in a table it cannot read or modify, so a snapshot can report which data it started from. The seed data and the verifier's expectation are written twice on purpose, so the judge does not trust the application's seeding code.

## L-050: Reduce with dependency-aware delta debugging and three-trial agreement

**Choice:** after a five-run baseline (at least four matching, no ERROR), run ddmin over operations, then faults, then retry suffixes. Removal closure deletes dependent operations, triggered faults, gated operations, and empty barriers. Each candidate is validated like user input, then must reproduce the target signature in three consecutive fresh trials, stopping at the first miss. It must also strictly improve (operations, faults, attempts). A final five-run validation decides between "verified" and "unverified".

**ELI5:** remove ingredients in groups; keep a removal only if the cake collapses the same way three times; then bake the final recipe five more times before telling anyone.

**Reason:** concurrency failures are noisy. A single matching trial would let luck shrink the plan, and a different failure must not stand in for the target. **Alternatives and tradeoffs:** statistical acceptance or global minimization would need many more trials than a laptop can afford. Shrinking payload values could change business meaning. The greedy search may miss smaller plans, so reports say "smaller", never "minimal".

## L-051: Treat budgets as part of the result and never let exhaustion look like a negative finding

**Choice:** count every started trial, including errors and rejections, against a run and wall budget that includes provisioning and cleanup. Reserve five trials (count and slowest-trial time) for final validation before admitting any candidate trial. Share a 1 GiB artifact budget across the group. Report `insufficient_budget` when the budget ends before any improvement, `reduction_unverified` when a better plan could not be validated, and `no_smaller_reproduction` only when the search actually finished.

**ELI5:** if the shop closes before you finish testing, you can't claim nothing in the shop would have worked.

**Reason:** the specification's defaults (50 trials, 10 minutes) are small compared with real Docker trials. Without the distinction, a budget stop would look like proof that the plan is already as small as possible. **Limit:** the time reserve uses the slowest observed trial; an even slower later trial can still exceed the wall budget, which is then recorded as the termination reason.

## L-052: Serve evidence read-only, loopback-only, behind a per-launch token in the URL fragment

**Choice:** `faultline serve` binds its own socket to 127.0.0.1 (no host option) and generates a random token for each launch. It prints a launch URL with the token after `#`. The middleware rejects wrong Host (403), foreign Origin (403), any method other than GET/HEAD (405), and API requests without the exact bearer token (401). It sends no CORS headers and applies a strict Content-Security-Policy. Artifacts are addressed by opaque inventory IDs and re-hashed before download.

**ELI5:** a museum case you can look into but not open, in a room whose key is handed out at the door and is gone if you walk out.

**Reason:** evidence can contain business data, and a local web server is reachable by every website open in your browser. Host checks stop DNS rebinding, Origin checks stop cross-site reads, and the token stops other local programs that do not have the launch URL. **Alternatives and tradeoffs:** cookies would survive reloads but are sent automatically, which reopens cross-site risks. A token in the query string would leak into logs and `Referer` headers. No authentication at all would rely only on Host/Origin. The cost is that a page reload loses access; the viewer explains how to reopen the URL rather than weakening the check.

## L-053: Separate unsupported from corrupt, and check history semantics as well as hashes

**Choice:** `read_bundle` raises `UnsupportedArtifact` for unknown bundle, report, or event versions (HTTP 409) and `EvidenceError` for damage (HTTP 422). It also rejects duplicate event IDs, truncated last lines, and report attempts without a recorded client outcome, even when the inventory hashes have been rewritten consistently.

**ELI5:** a letter in a language you cannot read is not the same as a torn letter. And a neatly re-stapled report that cites a page which doesn't exist is still wrong.

**Reason:** a newer bundle should not be called corrupt, and hashes only show that files match the inventory. **Limit:** this is integrity, not authenticity; someone who rewrites every file consistently can still forge a plausible bundle.

## L-054: Ship a dependency-light React viewer as packaged static assets

**Choice:** build a small React + TypeScript app (no router or state libraries) into `src/faultline/viewer/` and serve it from the wheel. It uses the History API for routes, keeps the token only in memory, and renders all artifact text through React's escaping. It paginates history, displays 64-bit values from the API's decimal strings, and gives every load state its own message.

**ELI5:** the display cards are printed in advance and shipped in the box, so visitors do not need a printing press.

**Reason:** reviewers should not need Node.js to look at evidence, and every third-party script would be another place untrusted code could come from. **Alternatives and tradeoffs:** server-rendered HTML would avoid JavaScript but make paginated timelines and keyboard navigation harder. A component library would speed up styling but add dependencies and assets to audit. The built assets are committed; rebuilding requires Node 20 (`web/`), and the committed bundle must be rebuilt whenever `web/src` changes.

## L-055: Make correct-B a different technique, not a configuration of correct-A

**Choice:** correct-B is a separate module using `SERIALIZABLE` transactions with bounded, jittered retries. It has no row locks and no unique indexes, writes absolute values, and rebuilds replay responses from the stored order row. Unit tests guard that it does not call A's functions or use `FOR UPDATE`.

**ELI5:** two students solve the same exam with different methods. If both get full marks, the exam is probably testing the subject, not one student's handwriting.

**Reason:** spec 04 requires an independent second implementation, so that an invariant that only holds because of A's locking style is exposed. **Alternatives and tradeoffs:** a flag inside A would share all of its assumptions. A second web framework would add dependencies without changing the transaction technique, which is the part that matters. B can still return 503 when its retries run out under heavy contention; scenarios retry on 503.

## L-056: Each known-bad variant removes exactly one safeguard, and races get room through declared faults

**Choice:** the defect variants are small, named deviations from correct-A (`DEFECTS` in `orders.py`). Race-dependent defects are exercised under a declared database-latency fault rather than by adding sleeps to the application.

**ELI5:** to test a smoke alarm you light a real match, not a picture of a flame. If the match is too small to notice, you hold it closer instead of painting smoke.

**Reason:** a single deviation makes the cause of each detection obvious and keeps the variants honest. The first probes showed `racy_idempotency` and `lost_update` passing because the handlers were too fast to overlap. Slow, recorded network conditions are a legitimate experiment variable; secret application sleeps would turn a real defect into a staged one. **Limit:** detection under latency says nothing about how often the race happens in production traffic.

## L-057: Expose the debit checkpoint through the worker, gated by declared variant capability

**Choice:** add one trigger event, `adapter_checkpoint`, usable only by `process_restart` and only for checkpoints an adapter declares, per variant. The application records reached checkpoints, bound to an attempt by the existing `X-Faultline-Attempt-Id` header, and pauses for a bounded time. The fault controller polls `/_faultline/checkpoints` through the worker while the bound attempt is in flight, records an `adapter_checkpoint` event, then crashes the application.

**ELI5:** to check what happens if the lights go out halfway through moving a box, you ask the mover to shout "halfway!" and then flip the switch, instead of flipping it at random and hoping.

**Reason:** the window between a debit commit and a credit commit is too narrow to hit reliably with timed faults. Spec 05 permits exactly this narrow, explicit seam. **Alternatives and tradeoffs:** a log-message trigger would be implicit and unverifiable. Letting the application call the coordinator would give it control-network access. Allowing correct variants to "pass" the checkpoint scenario would count a test that cannot exercise anything. The cost is real test-only code in the application. The checkpoint message only authorizes the crash; the violation still comes from raw rows.

## L-058: Bundle the transfer domain as an independent adapter rather than generalizing the order code

**Choice:** the transfer application, store helper, verifier, and adapter are separate modules with their own schema, fixture, and expected opening state. They are selected with `entry_point: bundled_transfers`. The engine gained only a registry of bundled adapters and the generic checkpoint trigger.

**ELI5:** a new subject gets its own textbook and answer key; the exam hall stays the same.

**Reason:** AT-023 requires the same engine to judge a second domain without order-specific core code. Sharing the order verifier's helpers would hide accidental coupling. **Limit:** the transfer domain has one correct implementation, so its correct-variant evidence is narrower than the order domain's.

## L-059: Gate CI on calibrated outcomes, not exit codes, with least privilege

**Choice:** three jobs: checks; a correct-variant gate; and a strict known-defect gate. Both Docker gates run through `faultline.calibration`, which judges each trial from its report. Actions are pinned to commit SHAs, the token is read-only, `pull_request_target` is not used, evidence is uploaded with `if: always()` for seven days from a non-hidden directory, and the viewer's committed build must match a fresh build.

**ELI5:** the smoke-alarm test passes when *the alarm* goes off for *the match*, not merely because something beeped.

**Reason:** a known-bad run exits 1 by design. Treating "exit 1" as success would also accept a different failure or a crash. **Alternatives and tradeoffs:** running all 20-trial matrices on every pull request would take hours on hosted runners; CI runs one trial per pair, and the 20-trial results are recorded release evidence. `upload-artifact` skips hidden paths by default, which is why CI writes to `ci-evidence/` rather than `.faultline/`. The CI workflow has not yet run on GitHub-hosted runners; its first real run may reveal timing differences on those machines.

## L-060: Report resource and frequency measurements with their machine and method

**Choice:** measurement and calibration outputs record the host's CPUs, memory, kernel, Docker version, sampling interval, every run's wall time, and leak counts. Peaks come from periodic sampling and are reported as lower bounds. Targets (60 s warm run, 10-minute PR matrix, 4-core/8 GiB budget) are compared against these measured values, never assumed.

**ELI5:** write down which kitchen and which stopwatch you used, or the baking times mean nothing to anyone else.

**Reason:** spec 10 asks for actual measurements and explicit scope changes if targets are missed. **Limit:** the reference Docker VM exposes 14 CPUs and about 7.2 GiB of memory. Memory is within the 8 GiB budget, but CPUs are not limited to 4, so timings may be faster than on a true 4-core host. Peak memory shows that the experiment fits the memory budget; it does not show how the experiment would perform on a 4-core host.

## L-061: Let drain helper calls share the stage budget, and raise the drain and observation defaults

**Choice:** the order drain's three observer helper calls now use the remaining drain budget instead of a fixed 5 s each. The graceful stop may use at most half of what remains. The default `drain_ms` and `observation_ms` rise from 10 s to 30 s, and the snapshot transport timeout is 60 s. Stage alarms still bound every step.

**ELI5:** if the bus is late because of traffic, you don't declare the passenger missing after five minutes; you wait as long as the whole trip allows.

**Reason:** the first 20-trial correct-A matrix (revision 973e27e) had 0 FAIL but 6 INCONCLUSIVE and 4 ERROR, all in the drain step. They clustered in two windows when every step on the host slowed 2-3x. ERRORs had not reached the first registry event: the hard-coded 5 s helper timeout expired while `docker exec` was starting Python. The verdicts were honest (no false PASS), but the harness was fragile. **Alternatives and tradeoffs:** retrying the helper would hide real hangs; dropping the evidence calls would weaken quiescence. Longer defaults lengthen the worst case of a genuinely hung run by up to 40 s. The pre-fix matrix is kept as evidence under `orders-correct-a-before-drain-fix`.

## L-062: Give probabilistic defects enough CI trials to be seen, and say so in the corpus

**Choice:** a corpus defect may declare `"detection": "probabilistic"` with `min_trials`. In `--strict` (CI) mode such a pair runs at least `min_trials` times and needs one detection with no harness ERROR. Deterministic defects still need a detection in every trial. The crash-between-commits order defect declares `min_trials: 15` (6/20 measured).

**ELI5:** you check a coin-flip bug several times before saying the alarm is broken; you check a guaranteed bug once.

**Reason:** a single CI trial would miss this defect about 70% of the time. CI would then fail randomly, and people would learn to ignore it. **Limit:** at the measured rate, 15 trials still miss about 0.5% of the time. The declaration is visible in the corpus file, not hidden in CI settings.
