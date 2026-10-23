# Checked adapter boundary: measured results

Validation date: 2026-10-23. These are observations from the internal adapter increment, not public-adapter or full M2 acceptance.

## Environment and commands

Execution used Debian WSL2 on a Windows host, Python 3.12, the locked project dependencies, and Docker Desktop Linux x86-64. The runtime was rebuilt after the source changes before integration execution.

Runtime image ID: `sha256:ac63b6637dfbaf3eb60524368e970f5b8ae09dd2a9bc9a0df298cc1e59a4cd1d`.

Host bundled Python-source inventory hash recorded by the ordinary run matrix: `8ea73160f4c00d344ae73ea2cdf04fcc61ffe47f492aaa65408a65c187814b38`.

Commands were invoked through WSL with the installed uv binary. Equivalent commands from the Linux workspace:

```bash
uv run pytest -q --junitxml=.faultline/adapter-unit-results.xml
uv run ruff check src tests
uv run ruff format --check src tests
docker build -t faultline-runtime:0.1.0 .
FAULTLINE_INTEGRATION=1 uv run pytest -x -s -q tests/test_integration.py \
  --basetemp=.faultline/adapter-validation \
  --junitxml=.faultline/adapter-validation-results.xml
```

Use a new `--basetemp` path for a future validation phase: pytest replaces that directory, so reusing it would erase retained evidence.

## Results

- **160 unit tests passed**, 13 Docker cases skipped by the default opt-out, in **8.03 seconds**. Ruff lint passed and its formatting check confirmed all 27 Python files were formatted.
- **13 Docker/PostgreSQL integration cases passed** in **198.91 seconds**. This includes all four ordinary/fault scenario-variant combinations, isolation and real payload conflict, the malformed-adapter case, final client timeout, missed required fault, SIGTERM, SIGKILL recovery, observation expiry, uncertain drain, and cleanup expiry/recovery.
- Read-only Docker inventory after completion showed **zero Faultline-managed containers and networks**.

One fresh run per matrix pair was performed in this phase:

| Scenario / variant | Observed verdict | Authoritative debit count | Measured wall time |
| --- | --- | --- | --- |
| control / unsafe | PASS | 1 | 12.80 s |
| control / correct | PASS | 1 | 11.18 s |
| lost-response / unsafe | FAIL | 2 | 12.03 s |
| lost-response / correct | PASS | 1 | 12.15 s |

All four runs had complete cleanup, the expected fault coverage, readable integrity-checked bundles, and matching adapter identities in the manifest/report/artifact. The unsafe fault case contains two 200000-paise debits; the correct case contains one. These four runs are regression evidence, not a new twenty-repetition acceptance campaign.

## Checking a broken judge against a real correct purchase

`test_malformed_adapter_finding_cannot_pass_real_purchase` temporarily changed the host adapter's verification hook to omit its last invariant result. The real corrected application committed one purchase and one debit under response suppression. Faultline retained the authoritative final snapshot but reported **ERROR**, with `adapter_contract_error` on `verify`, no accepted partial findings, and complete resource cleanup.

This is a deliberate in-memory test modification. The recorded source hash describes files on disk; it does not detect a Python monkeypatch. The ordinary matrix ran without that modification.

Unit coverage separately rejects missing/duplicated/unexpected invariant IDs, unsupported versions, conflicting logical identities, status/failure inconsistencies, missing evidence, oversized output, invalid snapshot metadata, setup without acknowledgment, and truthy non-boolean quiescence. It checks secret-safe diagnostics, propagation of deadlines/cancellation, capability refusal before Docker, and corruption detection for `adapter.json`.

## Evidence retained locally

- `.faultline/adapter-unit-results.xml`: final unit run results for this phase.
- `.faultline/adapter-validation-results.xml`: complete 13-case integration results.
- `.faultline/adapter-validation/`: run bundles, original interruption evidence, and recovery receipts under each test directory.
- Ordinary matrix run IDs: `61ccd878-16e4-4aab-9086-76fc061693e7`, `bdef36fe-ebbc-463b-8c4a-df3b5ec8edc8`, `bbbd5466-a7a9-45cd-a67e-81d82a78c9e6`, and `56ee57da-96d4-4793-847c-7143708a8b33` in table order.

The `.faultline` directory is ignored by Git. These files are local evidence rather than automatically published artifacts. Earlier acceptance measurements remain in their own documents.

## Scope not demonstrated

There is no externally registered adapter, external project manifest, second domain, concurrent workload, Toxiproxy fault, strict replay, reducer, React viewer, or packaged CI release in this increment. The existing parser, worker routing, and environment remain tied to the bundled order profile. A well-formed but incorrect verifier can still be wrong; result-shape validation complements independent domain tests rather than proving arbitrary adapter correctness.
