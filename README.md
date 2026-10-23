# Faultline

Faultline tests whether backend business rules survive retries and failures, using authoritative state rather than HTTP status alone.

**The first executable milestone is implemented:** one real PostgreSQL-backed purchase, a controlled lost response, a retry, and independent correctness checks. Both an unsafe backend and a transactionally idempotent backend are included. This is an early CLI implementation, not the complete v1 platform.

The stack is Python, FastAPI, PostgreSQL, and disposable Linux Docker containers. React remains planned for the later read-only report viewer. See the [full specification](specs/README.md), [implemented scope and limitations](docs/implementation-status.md), and [engineering principles](docs/engineering-principles.md) derived from the supplied field guide.

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
- `history.ndjson`: durable attempt, response witness, fault, client outcome, and lifecycle events.
- `snapshots/baseline.json` and `snapshots/final.json`: independently read business rows.
- `report.json` and `report.md`: verdict, expected/observed state, evidence references, and cleanup status.

```bash
.venv/bin/faultline report <run-uuid>
.venv/bin/faultline --format json report <run-uuid>
.venv/bin/faultline run examples/orders/lost-response.yaml --variant correct --repeat 5
.venv/bin/faultline clean --run <interrupted-run-uuid>
```

Global options (`--output`, `--format`, `--no-color`) precede the command. Reports are readable without Docker. `clean` preserves artifacts and refuses resources whose recorded IDs and ownership labels do not match. A hard-killed coordinator can leave a partial bundle and owned containers; use this scoped cleanup command, never a system-wide Docker prune.

Run/report exit codes: **0 PASS**, **1 FAIL**, **2 invalid input/preflight**, **3 INCONCLUSIVE**, **4 ERROR**, **130 interrupted**. Missing fault coverage cannot produce PASS. Cleanup failures produce ERROR while retaining available findings. Validation rejects features not implemented in this milestone.

## Verification

See [measured acceptance results](docs/acceptance-results.md) for the completed checks, retained evidence, and limits of the results.

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
FAULTLINE_INTEGRATION=1 uv run pytest -q tests/test_integration.py
```

The ordinary test command skips Docker integration explicitly. To execute the specification's twenty fresh runs for each of four scenario/variant pairs, plus real isolation, read-only-observer, payload-conflict, final-timeout, missed-fault, and SIGTERM cleanup checks:

```bash
FAULTLINE_INTEGRATION=1 FAULTLINE_ACCEPTANCE_REPEATS=20 \
  uv run pytest -x -s -q tests/test_integration.py
```

Every trial creates and removes its own database, application, worker, observer, and networks. Tests verify actual ledger totals, fault coverage, report integrity, and absence of owned leftovers. Independent verifier fixtures also test duplicate records, orphan effects, false success responses, lost updates, missing evidence, and a backend that performs no work.

Use only the bundled trusted images and synthetic fixtures. There is no arbitrary endpoint or production target option. The remaining release work—public adapters, concurrent scenario scheduling, Toxiproxy faults, strict replay, reduction, React, and CI packaging—is tracked in the specification and implementation-status document.
