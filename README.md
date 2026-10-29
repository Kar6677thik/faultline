# Faultline

Faultline tests whether backend business rules survive retries and failures, using authoritative state rather than HTTP status alone.

Faultline now includes concurrent experiments, materialized workloads, barriers and dependency gates, acknowledged database proxy faults and application restarts, and independent PostgreSQL verification. Milestone 2 is complete for the supported experiment engine; this is not the complete v1 release. The original unsafe/correct purchase demonstration remains a regression.

The stack is Python, FastAPI, PostgreSQL, and disposable Linux Docker containers. React remains planned for the later read-only report viewer. See the [full specification](specs/README.md), [implemented scope and limitations](docs/implementation-status.md), and [engineering principles](docs/engineering-principles.md) derived from the supplied field guide.

Learning the project? Start with the [plain-language learning guide](docs/learning/README.md): a complete purchase walkthrough, module explanations, glossary, decision log, and crash-recovery tutorial.

## Setup

Use Linux x86-64 or Debian/Ubuntu in WSL2 on Windows, Python 3.12 or 3.13, a local Docker Engine with Linux containers, and [uv](https://docs.astral.sh/uv/getting-started/installation/). Enable Docker Desktop integration for your WSL distribution. Run the following from this repository inside Linux/WSL; native PowerShell is not the execution platform.

```bash
uv sync --frozen
docker pull postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb
docker pull ghcr.io/shopify/toxiproxy@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e
docker build -t faultline-runtime:0.1.0 .
.venv/bin/faultline doctor
```

The first build/pull needs network access. Experiments use only locally available images and internal networks. Rebuild the runtime image after changes to `src/`, dependencies, or the Dockerfile; the host package and container must represent the same code revision. Docker image IDs are recorded in every run manifest. The multi-stage image contains the installed package and runtime dependencies, not the build toolchain.

## Demonstrate the defect and correction

```bash
.venv/bin/faultline validate examples/orders/lost-response.yaml
.venv/bin/faultline run examples/orders/control.yaml --variant unsafe
.venv/bin/faultline run examples/orders/lost-response.yaml --variant unsafe
.venv/bin/faultline run examples/orders/lost-response.yaml --variant correct
```

Expected outcomes: the ordinary unsafe purchase passes; the unsafe lost-response experiment fails with two ₹2,000 debits for one purchase; the corrected version passes with one debit. The unsafe experiment deliberately exits **1**, so a shell script using `set -e` must handle that expected status explicitly. A harness error is a different outcome and must never be accepted as successful defect detection.

The fault receives a genuine complete successful response, records its evidence, withholds it from the client retry policy, and reports a timeout at the attempt deadline. It is **controlled client response suppression**, not a TCP packet-loss experiment. The verifier then reads the real database using read-only credentials.

Each run prints its UUID and writes a bundle to `.faultline/runs/<uuid>/`:

- `plan.json` and `manifest.json`: effective scenario, versions, resolved image IDs, ownership records.
- `adapter.json`: selected capabilities, package version, and content fingerprint. Installed adapters also record their factory, installed-file hashes, and shared engine source identity.
- `project.json` for project-backed runs: validated project settings and their configuration identity.
- `history.ndjson`: durable attempt, response witness, fault, client outcome, and lifecycle events.
- `snapshots/baseline.json` and `snapshots/final.json`: independently read business rows.
- `report.json` and `report.md`: verdict, expected/observed state, evidence references, and cleanup status.

```bash
.venv/bin/faultline report <run-uuid>
.venv/bin/faultline --format json report <run-uuid>
.venv/bin/faultline run examples/orders/lost-response.yaml --variant correct --repeat 5
.venv/bin/faultline clean --run <interrupted-run-uuid>
.venv/bin/faultline clean --stale
```

Global options (`--output`, `--format`, `--no-color`) precede the command. Reports are readable without Docker. `clean` preserves artifacts and refuses resources whose recorded IDs and ownership labels do not match. A hard-killed coordinator can leave a partial bundle and owned containers; use scoped cleanup, never a system-wide Docker prune.

Use exactly one cleanup selector: `--run` for one recorded run or `--stale` for recorded runs in the selected output root. Cleanup refuses an active root lock, validates the entire selection before mutation, and writes separate receipts under `.faultline/cleanup/`. It never rewrites an original report or turns an unfinished experiment into PASS. Stale scanning is capped at 1,000 directory entries; malformed selections are refused, so use `--run` to recover another valid bundle. See the [recovery guide](docs/learning/recovery.md) for commands, failure meanings, and remaining limits.

Recovery must use the original output root recorded in the manifest. Copied/moved bundles and legacy bundles without `execution_root` are refused for automatic cleanup because their original lock cannot be established. Completed reports remain portable and readable without this recovery metadata.

New runs save creation intents before contacting Docker. If a crash loses the returned resource ID, cleanup can inspect the exact recorded name, verify all ownership and creation-token labels, and durably bind the ID before deletion. Later recovery keeps those IDs in `<output>/cleanup/bindings/`; preserve that directory with the manifests and receipts. An absent unresolved creation remains incomplete because Docker could still finish the request later. See the [ELI5 creation recovery guide](docs/learning/creation-recovery.md).

## Replay and compare a saved run

```bash
.venv/bin/faultline replay <run-uuid> --repeat 3 --signature <signature>
.venv/bin/faultline compare <run-uuid> --variant correct --repeat 2
.venv/bin/faultline report <group-uuid>
```

Strict replay reruns the saved plan in fresh containers and refuses (exit 2, before provisioning) if the engine, adapter content, plan, platform, or any image ID differs. Comparison runs the same plan against an explicitly chosen variant and records each difference. Groups under `.faultline/groups/` count every trial, report matching failures per started and per conclusive trial, and never count an ERROR or interrupted run as a reproduction. An unsafe duplicate usually has several violation signatures; choose one with `--signature` to get reproduction fractions. See the [replay guide](docs/learning/replay.md).

## Reduce a failure

```bash
.venv/bin/faultline run examples/orders/reduction-market.yaml --variant unsafe
.venv/bin/faultline reduce <run-uuid> --signature <signature> --max-runs 80 --max-duration-ms 3600000
```

Reduction deletes whole operations, faults, and retry suffixes, together with anything that depends on them. A deletion is kept only if the selected violation still reproduces in three fresh runs; the best candidate is then validated five more times. Reduce exit codes: **0** verified or no smaller reproduction; **2** invalid input; **3** unstable baseline, unverified, or insufficient budget; **4** harness error. Results say "smaller reproduction", never "minimal". See the [reduction guide](docs/learning/reduction.md).

Run/report exit codes: **0 PASS**, **1 FAIL**, **2 invalid input/preflight**, **3 INCONCLUSIVE**, **4 ERROR**, **130 interrupted**. Missing fault coverage cannot produce PASS. Cleanup failures produce ERROR while retaining available findings. Validation rejects features not implemented in this milestone.

The engine uses a checked order-adapter boundary. `faultline --format json validate examples/orders/control.yaml` shows its supported capabilities without Docker. Incomplete or malformed grading results produce ERROR, even if the returned subset says PASS. Projects can select the built-in adapter or a separately installed trusted Python package through the `faultline.adapters` entry-point group. Exact package versions, unambiguous registrations, bounded file provenance, and a ten-second adapter preflight are checked before Docker. See the [ELI5 adapter guide](docs/learning/adapters.md) and [installation tutorial](docs/learning/installed-adapters.md).

## Configure a project

The [runnable project example](examples/orders/faultline.project.yaml) declares the five-service order environment, variant images, container resources, and project budget ceilings:

```bash
uv run faultline --project examples/orders/faultline.project.yaml --format json validate lost-response.yaml
uv run faultline --project examples/orders/faultline.project.yaml doctor
uv run faultline --project examples/orders/faultline.project.yaml run lost-response.yaml --variant correct
```

With a project, the scenario path is relative to the project file's directory and must stay inside it. Output remains relative to the current directory unless `--output` is supplied. Without `--project`, validate/run/doctor discover `faultline.project.yaml` in the current directory; if absent, the original implicit demo remains available. An invalid discovered manifest is an error. A project with only one declared variant permits omitting `--variant`.

Project limits cap scenario budgets. Omitted scenario defaults are clipped to those ceilings; explicit over-ceiling requests are rejected. JSON validation shows effective limits and the plan/configuration hashes. Docker enforces declared memory, CPU, and PID limits. Preflight resolves selected local application images to immutable IDs and checks architecture/host capacity without pulling images.

The engine uses five owned service roles and four internal networks. Applications reach PostgreSQL through the pinned Toxiproxy; the observer reaches it independently. Local compatible images are resolved to immutable IDs before provisioning. Arbitrary endpoints, host mounts, privileged services, and production targets are not accepted.

See [the experiment-engine learning guide](docs/learning/experiment-engine.md) for plan materialization, bounded concurrent admission, barriers, conditional retries, fault acknowledgments, and in-flight registration. The [external adapter tutorial](docs/learning/external-adapters.md) documents public hooks and runs a separately packaged counter application with its own database schema and verifier.

Offline reporting and scoped cleanup use recorded evidence without importing a project adapter. The older inherited order-adapter example remains useful for packaging and malformed-grader checks; the independent counter example exercises actual domain onboarding.

## Time budgets

Scenarios now accept stage and total limits. For example, this `limits` section allows at most 90 seconds per run while reserving 30 seconds for cleanup and reporting:

```yaml
limits:
  provisioning_ms: 60000
  execution_ms: 60000
  drain_ms: 10000
  observation_ms: 10000
  verification_ms: 10000
  cleanup_ms: 30000
  total_ms: 90000
```

The remaining total budget may shorten a stage; maxima are not guaranteed allocations. The timer starts after preflight and lock acquisition. Execution expiry stops new work but permits bounded observation where time remains. Verification or cleanup expiry produces ERROR; unfinished/uncertain business work cannot receive a full PASS. Reports include deadline diagnostics and stage timing. See the [ELI5 deadline guide](docs/learning/deadlines.md) for the arithmetic, outcomes, and limitations.

The Linux/WSL coordinator runs on the main Python thread and refuses to take over an existing process alarm. Very small budgets or disk/daemon failures can leave partial evidence or recorded leftovers for scoped recovery. Standalone recovery commands and entire repeat groups do not yet have a configurable total deadline.

## Verification

See the [M2 acceptance results](docs/m2-acceptance-results.md) for the 38-case full Docker regression, final four-case follow-up, and 299 passing unit tests. Earlier [initial](docs/acceptance-results.md), [recovery](docs/recovery-acceptance-results.md), [deadline](docs/deadline-acceptance-results.md), [adapter](docs/adapter-acceptance-results.md), [project](docs/project-acceptance-results.md), [installed-adapter](docs/installed-adapter-acceptance-results.md), and [creation-recovery](docs/creation-recovery-acceptance-results.md) results retain their separate measurements.

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
FAULTLINE_INTEGRATION=1 uv run pytest -q tests/test_integration.py
```

The ordinary test command skips Docker integration explicitly. The integration suite also uses `uv` to build a separate example wheel and installs it into a temporary directory without dependencies; first build may fetch build tooling. To execute the specification's twenty fresh runs for each of four scenario/variant pairs, plus real isolation, read-only-observer, payload-conflict, final-timeout, missed-fault, signal/recovery, stage-deadline, and installed-adapter checks:

```bash
FAULTLINE_INTEGRATION=1 FAULTLINE_ACCEPTANCE_REPEATS=20 \
  uv run pytest -x -s -q tests/test_integration.py
```

Every trial creates and removes its own database, application, proxy, worker, observer, and networks. Tests verify actual ledger totals, fault coverage, report integrity, and absence of owned leftovers. Independent verifier fixtures also test duplicate records, orphan effects, false success responses, lost updates, missing evidence, and a backend that performs no work.

Use only trusted local compatible images and synthetic fixtures. There is no arbitrary endpoint or production target option. Two milestones remain: M3 has strict replay, comparison, and reduction, and still adds the read-only API and React investigation; M4 adds independent correct-B/transfer validation, the defect corpus, and CI/release packaging. See the specification and implementation-status document for the remaining gates.
