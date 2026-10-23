# CLI, local API, and React viewer

All commands below are intended interfaces, not currently available software. CLI contract version: 1. The installed executable will be `faultline`.

## CLI commands

Global options are `--project PATH` (default `./faultline.project.yaml`), `--output PATH` (default `./.faultline`), `--format text|json` (default text), and `--no-color`. Paths are explicit arguments; command output MUST NOT require a terminal to be useful. JSON mode emits one final result object to stdout, with progress/diagnostics on stderr. It never mixes progress text into JSON output.

- `faultline doctor`: check supported platform, local Docker connection, available resource budget, image/version compatibility, and installation. It creates no experiment resources and does not pull images automatically. Missing prerequisites produce actionable diagnostics.
- `faultline validate SCENARIO`: validate project, adapter capabilities, scenario, bounds, and expanded plan without creating AUT resources. Installed adapter code may be loaded because validation is a trusted local operation. It does not claim database readiness or baseline validity.
- `faultline run SCENARIO --variant NAME [--seed N] [--repeat N]`: run the materialized scenario against the selected project variant. `--variant` is required if multiple variants exist; otherwise use the sole variant. Seed overrides are recorded. Repeat defaults to 1; values 1–100 are accepted. All repeats use one plan and fresh fixtures, not a new seed per repeat.
- `faultline replay RUN_ID_OR_BUNDLE [--repeat N]`: strict replay with default five repetitions; no variant/image override is accepted. Reject missing or incompatible source inputs before provisioning.
- `faultline compare RUN_ID_OR_BUNDLE --variant NAME [--repeat N]`: execute the saved plan against an explicitly chosen project variant under the installed compatible contracts. Default five repetitions. Record all engine, adapter, image, and platform changes. If invariant semantics or fixture identity are incompatible, comparison is invalid rather than producing an unqualified before/after claim.
- `faultline reduce RUN_ID_OR_BUNDLE [--signature ID] [--max-runs N] [--max-duration-ms N]`: reduce the selected failure under the algorithm/budget in the evidence specification. If multiple signatures exist, require an explicit selection and print the choices. No interactive selection is permitted in CI.
- `faultline report RUN_ID_OR_BUNDLE [--format text|json|markdown]`: inspect verified existing evidence without loading adapter code or starting Docker. For a group ID, show the complete outcome breakdown.
- `faultline serve [--runs-dir PATH] [--port N]`: serve packaged React assets and a read-only API on `127.0.0.1`; default port 8765, fail clearly if occupied. With `--port 0`, select a free port and print its address. Serve only the chosen artifact root, defaulting to `<output>/runs`. Opening a browser is optional via `--open`.
- `faultline clean --run RUN_ID`: remove only leftover resources recorded for that run and verified by ownership labels. It preserves artifacts. `faultline clean --stale` considers only non-live runs in the selected local output root; uncertain ownership or a live lock is refused. There is no system-wide prune operation.

Artifact IDs and bundle paths are distinct argument forms: UUIDs resolve within the output root; a filesystem path is normalized and validated as a bundle. Reports MAY inspect an explicitly chosen outside bundle, but viewer requests cannot select arbitrary paths. Import/download/archive execution is not supported in v1.

## Exit codes and aggregation

For run/replay/compare/report: 0 means PASS; 1 means at least one FAIL with no ERROR; 2 means invalid input, unsupported version, or preflight rejection before a run; 3 means INCONCLUSIVE with no FAIL/ERROR; 4 means ERROR. A group uses ERROR > FAIL > INCONCLUSIVE > PASS precedence and reports counts for all members. Mixed pass/fail is never aggregate PASS.

For validate/doctor/clean: 0 means the requested operation completed, 2 means invalid request or failed preflight, and 4 means an unexpected operational error. Cleanup refusal due to an active run is code 2.

For reduce: 0 means `reduction_verified` or `no_smaller_reproduction` after stable baseline confirmation; 3 means `baseline_unstable`, `reduction_unverified`, or insufficient budget to complete a meaningful result; 2 means invalid input; 4 means a harness error that prevents continuing. The reduction report preserves any per-trial errors even if the search continues, and never presents those trials as conclusive.

SIGINT/cancellation returns 130 for the command, after bounded cleanup, regardless of retained invariant findings. Individual run reports keep their independently computed verdict and interrupted lifecycle. SIGTERM uses the same orderly shutdown where available; forced termination may leave an incomplete bundle. `serve` returns 0 on orderly shutdown and 4 on an unexpected server error.

Human output MUST include run ID, verdict, invariant failure/unknown counts, required fault coverage, artifact path, and cleanup status. A failing run additionally prints the key violation and a proposed replay command. Replay/reduction suggestions are omitted when evidence compatibility is incomplete.

The JSON command envelope has `cli_schema_version`, `command`, `outcome`, `exit_code`, `run_ids`, optional `group_id`, `artifact_paths`, `summary`, and `diagnostics`. Diagnostics have stable `code`, readable `message`, optional `field_path`, and bounded structured details. Do not parse human messages to make CI decisions.

## Read-only report API

The FastAPI server reads bundles and serves static frontend assets. It never starts experiments, kills processes, modifies manifests, imports adapters, or accepts arbitrary filesystem paths. Default API version is `/api/v1`. JSON uses UTC timestamps, integer millisecond durations, and the report's existing numeric units.

Endpoints are:

- `GET /health`: server readiness and API version; no filesystem details.
- `GET /api/v1/runs?limit=50&cursor=...&verdict=...`: stable paginated summaries sorted by creation time then run ID, newest first. Limit range is 1–200. Verdict filter accepts the four verdicts plus `RUNNING` and `INTERRUPTED` for unfinished bundles. An opaque cursor includes the listing snapshot boundary so newly added runs do not reorder subsequent pages.
- `GET /api/v1/runs/{run_id}`: manifest summary, report, artifact inventory, and current completeness state.
- `GET /api/v1/runs/{run_id}/events?after_sequence=0&limit=200`: sequence-ordered events with `next_after_sequence` and `has_more`; limit range 1–1000. Incomplete trailing lines are excluded with a diagnostic; the run is not portrayed as complete.
- `GET /api/v1/runs/{run_id}/invariants`: invariant results with evidence references and property versions.
- `GET /api/v1/runs/{run_id}/snapshots/{snapshot_id}?record_set=...&offset=0&limit=100`: metadata plus canonical record rows; record set must exist in the inventory and be adapter-declared. Limit maximum 1000. Snapshots are immutable, so offset pagination is stable.
- `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}`: download one inventoried safe artifact by opaque ID, never by caller-supplied path. Logs use plain text, JSON uses application/json, and Markdown is downloaded as text. Attachment headers prevent active content rendering.
- `GET /api/v1/groups/{group_id}`: replay/comparison/reduction summary, trial references, target signature, budgets, candidate decisions, and outcomes. Group records live under `<output>/groups` and may be exposed only when this sibling root was selected by the local CLI, never derived from request input.

Responses use `{data, meta}`; errors use `{error: {code, message, details}}`. `meta` includes API/report versions and pagination where relevant. Status codes: 200 success, 304 for matching ETag, 400 malformed query, 401 missing/invalid local access token, 403 rejected origin/host, 404 unknown ID, 409 incompatible artifact version, 422 corrupt evidence, and 500 unexpected server failure. Missing snapshots during an active run are 404 with `artifact_not_ready`, not proof that no business state exists. Mutation methods return 405.

The server generates a fresh cryptographically random local bearer token on startup, passed to the browser in the URL fragment and kept in memory. API fetches send it as an Authorization bearer token; the UI removes the fragment from the address bar. A full page reload loses access and displays an instruction to reopen the launch URL printed by `serve`; it must not weaken authentication to make reload work. The token is never stored in artifacts, query strings, analytics, or server logs. A terminal user may retrieve the launch URL from the `serve` process output. This local token is generated by the application, not requested from the user.

The server MUST validate Host and Origin, reject non-loopback bindings, disable CORS, and serve no third-party assets. Token-authenticated same-origin fetches and token-authenticated CLI access without Origin are allowed; a present foreign Origin is rejected. Static assets contain no run data. The viewer security requirements are further specified in [safety](09-safety.md).

## React investigation flow

The run list shows scenario, variant, creation time, lifecycle/verdict, required fault coverage, and replay/comparison lineage. Filtering and sorting MUST remain usable without loading every event. Each row opens a stable run route.

The run view prioritizes the outcome and its limitations: violated or unknown invariants, expected/observed values with units, affected logical operations, and cleanup problems. A PASS prominently states that only the listed properties and experiments were checked. ERROR with an attached violation shows both, not a generic infrastructure screen that hides evidence.

The timeline differentiates intended schedule from observed execution, displays attempt/retry identities, upstream witness versus client-visible outcome, actual fault acknowledgments, and observation boundaries. It MUST not show synchronized wall time as proof of causality. Default ordering is durable sequence; per-source time and causal references are inspectable.

An invariant detail view links to exact ledger/order/transfer rows, corresponding requests, and relevant events. A state comparison shows baseline and final values, including missing/duplicate records. Values MUST preserve integer precision; React must display any 64-bit value that exceeds JavaScript safe integer range from a string serialization contract, not round it. JSON APIs encode money, nanoseconds, and other potentially unsafe 64-bit integers as decimal strings; counts and bounded millisecond durations remain JSON numbers. Persisted Python artifacts retain integer types; the API makes this documented presentation conversion.

Replay groups show all outcome counts and both reproduction denominators. Comparison views highlight environment/variant differences and avoid claiming replay equivalence. Reduction views show original versus candidate objective counts, chosen signature, trial results, budget, and whether final validation completed.

Loading, empty, unavailable, corrupt, unsupported-version, incomplete, and disconnected states MUST be distinct. Use labels/icons as well as colors. All primary controls and evidence links must work by keyboard, focus must remain visible, and status updates must be announced accessibly. Support desktop widths from 1024 px and readable narrow layouts down to 375 px. Large histories require pagination or virtualization; rendering an entire 50 MiB history into the DOM is prohibited.

The viewer polls active runs at a bounded interval of at least two seconds and stops when final. Live execution controls, editing scenarios, accounts, sharing links, charts of unsupported benchmark claims, and a decorative dashboard without evidence navigation are outside v1.
