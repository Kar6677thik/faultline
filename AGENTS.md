# Working on Faultline

Read `README.md` and `docs/implementation-status.md` before changing behavior. The files in `specs/` describe the target v1 release; do not present deferred features as implemented.

Apply `docs/engineering-principles.md`, derived from the user's Software Engineering Field Guide: simple modules, explicit contracts and dependencies, independent authoritative verification, bounded work, least privilege, and tests of real failure behavior. Introduce patterns or infrastructure only when a concrete requirement needs them.

- Use Python 3.12/3.13 and `uv.lock`; execute Docker experiments on Linux x86-64 or WSL2.
- Keep verifier logic independent of the reference application's transaction implementation and variant name.
- Never add arbitrary endpoint targeting or mutate resources without matching recorded ownership and labels.
- Preserve the distinction between business FAIL, evidence INCONCLUSIVE, and harness ERROR.
- Keep meaningful unit tests and real PostgreSQL integration coverage. `uv run pytest -q` skips Docker tests explicitly; opt in with `FAULTLINE_INTEGRATION=1` after building the runtime image.
- Run `uv run ruff check src tests` and `uv run ruff format --check src tests` for Python changes. Run tests appropriate to the changed behavior.
- Rebuild `faultline-runtime:0.1.0` after runtime source/dependency changes before integration testing. Repetitions use fresh fixtures; they are not deterministic replay.
- Update the implementation-status document when scope, supported contracts, or limitations change. Record measured outcomes separately from targets.
