# Faultline

Faultline tests whether backend business rules survive retries and failures, using authoritative state rather than HTTP status alone.

**The first executable milestone is implemented:** one real PostgreSQL-backed purchase, a controlled lost response, a retry, and independent correctness checks. Both an unsafe backend and a transactionally idempotent backend are included. This is an early CLI implementation, not the complete v1 platform.

The stack is Python, FastAPI, PostgreSQL, and disposable Linux Docker containers. React remains planned for the later read-only report viewer. See the [full specification](specs/README.md), [implemented scope and limitations](docs/implementation-status.md), and [engineering principles](docs/engineering-principles.md) derived from the supplied field guide.

Learning the project? Start with the [plain-language learning guide](docs/learning/README.md): a complete purchase walkthrough, module explanations, glossary, decision log, and crash-recovery tutorial.

## Setup

Use Linux x86-64 or Debian/Ubuntu in WSL2 on Windows, Python 3.12 or 3.13, a local Docker Engine with Linux containers, and [uv](https://docs.astral.sh/uv/getting-started/installation/). Enable Docker Desktop integration for your WSL distribution. Run the following from this repository inside Linux/WSL; native PowerShell is not the execution platform.

```bash
uv sync --frozen
docker pull postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb
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

Run/report exit codes: **0 PASS**, **1 FAIL**, **2 invalid input/preflight**, **3 INCONCLUSIVE**, **4 ERROR**, **130 interrupted**. Missing fault coverage cannot produce PASS. Cleanup failures produce ERROR while retaining available findings. Validation rejects features not implemented in this milestone.

The engine uses a checked order-adapter boundary. `faultline --format json validate examples/orders/control.yaml` shows its supported capabilities without Docker. Incomplete or malformed grading results produce ERROR, even if the returned subset says PASS. Projects can select the built-in adapter or a separately installed trusted Python package through the `faultline.adapters` entry-point group. Exact package versions, unambiguous registrations, bounded file provenance, and a ten-second adapter preflight are checked before Docker. See the [ELI5 adapter guide](docs/learning/adapters.md) and [installation tutorial](docs/learning/installed-adapters.md).

## Configure a project

The [runnable project example](examples/orders/faultline.project.yaml) declares the current four-service order environment, variant images, container resources, and project budget ceilings:

```bash
uv run faultline --project examples/orders/faultline.project.yaml --format json validate lost-response.yaml
uv run faultline --project examples/orders/faultline.project.yaml doctor
uv run faultline --project examples/orders/faultline.project.yaml run lost-response.yaml --variant correct
```

With a project, the scenario path is relative to the project file's directory and must stay inside it. Output remains relative to the current directory unless `--output` is supplied. Without `--project`, validate/run/doctor discover `faultline.project.yaml` in the current directory; if absent, the original implicit demo remains available. An invalid discovered manifest is an error. A project with only one declared variant permits omitting `--variant`.

Project limits cap scenario budgets. Omitted scenario defaults are clipped to those ceilings; explicit over-ceiling requests are rejected. JSON validation shows effective limits and the plan/configuration hashes. Docker enforces declared memory, CPU, and PID limits. Preflight resolves selected local application images to immutable IDs and checks architecture/host capacity without pulling images.

This is a restricted order-profile manifest, not the complete v1 project/adapter API. The helper/database images, role bindings, route, permissions, and readiness methods remain constrained. Installed adapters must implement this same bridge; proxy topology, additional domains, custom environment mappings, extra writable paths, and readiness overrides remain unsupported. A custom local application image must conform to the existing order fixture. Read the [ELI5 project guide](docs/learning/projects.md) for exact settings, image requirements, path rules, and examples.

Offline reporting and scoped cleanup use recorded evidence and ownership without loading project YAML or adapters. They remain usable when the project file changes or the adapter package is uninstalled. The [separate example package](examples/installed-orders-adapter/pyproject.toml) demonstrates this bridge using the existing order hooks; it is not an independent verifier or application.

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

See the [installed-adapter acceptance results](docs/installed-adapter-acceptance-results.md) for the latest validation, and the earlier [initial](docs/acceptance-results.md), [recovery](docs/recovery-acceptance-results.md), [deadline](docs/deadline-acceptance-results.md), [adapter](docs/adapter-acceptance-results.md), and [project](docs/project-acceptance-results.md) results for their measured phases and retained evidence.

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

Every trial creates and removes its own database, application, worker, observer, and networks. Tests verify actual ledger totals, fault coverage, report integrity, and absence of owned leftovers. Independent verifier fixtures also test duplicate records, orphan effects, false success responses, lost updates, missing evidence, and a backend that performs no work.

Use only trusted local application images and synthetic fixtures; dependency/helper images remain constrained to the bundled profile. There is no arbitrary endpoint or production target option. The remaining release work—public adapters, concurrent scenario scheduling, Toxiproxy faults, strict replay, reduction, React, and CI packaging—is tracked in the specification and implementation-status document.
