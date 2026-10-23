# Why Faultline is built this way

These are implementation decisions, explained for learning. They complement the [release-level decisions](../../specs/11-decisions.md); a future requirement may change a choice. Each entry states what works now, why we chose it, alternatives, and the cost. All entries below were documented on 2026-10-23.

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

**Alternative and cost:** there is a small unavoidable gap between Docker creating a resource and the coordinator persisting its returned ID. A kill in that gap can leave an unrecorded resource that this tool refuses to recover. Future creation/reconciliation work needs its own authorization design. Per-request Docker timeouts exist, but a whole recovery-command deadline and guaranteed machine-crash durability are still deferred.

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

**Alternative and cost:** unbounded emergency reporting would violate the total budget. A very small budget, full disk, or delayed signal can therefore leave only a partial bundle. Timing in a report ends before report assembly; we do not fabricate finalization duration before it has finished. Standalone recovery command deadlines and creation-record recovery remain separate future work.

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
