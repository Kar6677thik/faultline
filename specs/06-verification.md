# Correctness and verdicts

## Evidence and independence

The verifier is a pure, versioned function of the plan, actual history, validated baseline, and authoritative observations. It MUST preserve duplicate/orphan records and check cross-record consistency. It MUST NOT trust an AUT's `correct: true`, reconstruct missing rows from successful responses, or derive business completion from HTTP status alone.

Each invariant returns `PASS`, `FAIL`, or `UNKNOWN`, with invariant ID/version, checked logical identities, explanation, expected/observed values, evidence references, and sufficiency requirements. `UNKNOWN` includes a machine-readable reason such as `snapshot_incomplete`, `not_quiescent`, `history_gap`, or `unsupported_evidence`. An empty set of checked operations cannot establish success for a scenario that required work.

A final complete PostgreSQL snapshot is required for conservation, atomicity, and completion checks. A coherent earlier snapshot may establish append-only duplicate debit existence if both distinct committed ledger entries are visible and the profile forbids reversal/deletion. Each such finding states its observation boundary. This does not justify running unrelated checks on partial state.

Malformed/truncated evidence, inconsistent hashes, or incompatible contracts are harness errors. A well-formed snapshot explicitly marked incomplete yields UNKNOWN for affected checks. Adapter exceptions are errors, not business failures.

## Order invariant definitions

For each account/purchase pair P, let O(P) be all confirmed orders, D(P) all debit ledger entries, M(P) all inventory movements, and E(P) all confirmation outbox intents in the final snapshot. Let the canonical purchase amount be fixture price multiplied by quantity. Records are grouped by business identity, not just idempotency key; a changed or omitted key cannot hide duplicate effects.

- `orders.at_most_one_debit`: count D(P) must be at most one and its amount, when present, must equal the canonical amount. Two distinct committed debit entries for one P are a definitive violation even if HTTP responses were lost. A zero debit is not enough to pass completion.
- `orders.purchase_atomicity`: a completed purchase has exactly one O(P), one D(P), and one matching M(P); all IDs, account, amount, SKU, and quantity agree. A logical purchase that never committed has none of these records. A rejected conflicting attempt against an existing purchase must create no additional effects; the existing successful purchase remains valid. Debit without order, order without debit, duplicate orders, or orphan/mismatched records fail at quiescence.
- `orders.wallet_conservation`: for every fixture account, current balance equals opening balance plus all credits minus all debits, and is nonnegative. Aggregate over the entire snapshot, including records not associated with known requests; unexpected records must be reported. Equality alone cannot detect double charging; it complements the identity checks.
- `orders.inventory_conservation`: final stock equals opening stock plus the sum of signed movements and is nonnegative. Each successful quantity corresponds to one movement of negative quantity. Unrelated/orphan mutations fail the profile contract.
- `orders.confirmation_intent`: exactly one `order_confirmed` outbox intent exists for every confirmed order, with matching canonical payload; no intent exists for an absent/rejected order. This says nothing about broker delivery or consumer exactly-once processing.
- `orders.idempotency_contract`: same account/key and canonical payload converge on the same business result; conflicting payloads have the expected rejection and no additional effects. Check both visible successes and suppressed upstream response witnesses, plus idempotency records and business state. Known successful responses that identify nonexistent or conflicting orders fail. Missing response evidence for a required response comparison is UNKNOWN, not assumed equality.
- `orders.completed_purchase`: a logical purchase with expected success, whose required attempts ran and whose faults healed, must be confirmed exactly once by the observation deadline. A purchase that never commits after bounded recovery fails this declared progress requirement. A final timeout does not automatically fail if authoritative state proves completion. If work is still in progress or state cannot be observed, return UNKNOWN. Declared expected rejections must have a visible matching rejection and no additional business effects attributable to the rejected operation; a timeout cannot prove correct rejection. Valid existing effects for another successful operation sharing that purchase identity are not forbidden.

Before fixture state becomes the baseline, validate opening balances, stock, price, currency, empty business tables, and schema compatibility. A corrupted fixture is ERROR, not an AUT violation. No tolerance or floating point rounding applies to money.

## Transfer invariant definitions

- `transfers.conservation`: sum of account balances equals opening total; each account also reconciles to its ledger and remains nonnegative.
- `transfers.atomicity`: every committed transfer has exactly one source debit and one destination credit of equal positive amount, correct currency, and correct parties; no one-sided, duplicate, or orphan effects exist.
- `transfers.idempotency_contract`: identical retries produce the same transfer/result; conflicting keys or identities reject without new effects.
- `transfers.completed_transfer`: expected successful transfers commit exactly once by the bounded observation point; expected rejections have no effects and a matching visible response.

These invariants use a separate adapter projection, not fabricated order records. Whole-state checks inspect all observed business records so an uncorrelated mutation cannot evade detection.

## Run result and precedence

A run report contains three independent dimensions: lifecycle (`completed` or `interrupted`), invariant findings, and harness/coverage diagnostics. Its overall verdict is computed in this order:

1. **ERROR:** trusted evidence is corrupt, an adapter/controller crashes, infrastructure setup fails, a required controller action fails, the worker stream is lost, or cleanup fails. Confirmed invariant violations, if any, remain first-class findings.
2. **FAIL:** no harness error and at least one invariant has a sufficiently evidenced FAIL. Other checks may remain UNKNOWN and requested faults may be missed; the report must disclose both. This proves a business violation but may not validate the intended fault experiment.
3. **INCONCLUSIVE:** no conclusive violation or harness error, but a required fault was not applied, required work was not dispatched, a required invariant is UNKNOWN, observation/drain deadline expired, coverage is incomplete, the run was cancelled, or the effective plan did not finish.
4. **PASS:** the entire intended plan completed, all required faults were applied and healed, every required invariant returned PASS on sufficient evidence, and no harness/cleanup error occurred.

An execution deadline while requests remain unsubmitted is INCONCLUSIVE. A valid bounded completion check on finished, fully observed work can FAIL for missing business progress. Provisioning failure is ERROR. Cancellation alone is INCONCLUSIVE, but cancellation after an independently proven violation may be FAIL; the process still reports interruption as described by the CLI contract.

Control scenarios with no faults may PASS. A run with optional faults unapplied may PASS but MUST list them prominently as unapplied. PASS MUST always display its property set, observed workload, and coverage; a green badge cannot imply universal reliability.

## Failure signatures and explanations

A stable violation signature contains invariant ID/version, violation code (for example `duplicate_debit`), adapter profile/version, and the failing logical business identity. It excludes volatile row IDs, timestamps, amounts observed as a consequence, or the number of unrelated operations. A reducer MUST target a selected signature, not merely any FAIL. A different defect found during reduction is retained separately and does not satisfy the target.

Diagnostic wording is template-based in v1. For a duplicate debit, include logical purchase identity, expected count/amount, observed ledger entry IDs/count/total, associated attempts, known client outcomes, and applied faults. Identify supporting evidence and uncertainty. If the server committed and the client retried, say so only when those observations are actually supported. Histories show temporal relationships; they do not prove exclusive causation.

## Calibration

The verifier MUST be tested with hand-audited canonical snapshots and histories independently of AUT code. Include duplicates, orphan records, lost updates, inconsistent balances, no-op backends, payload conflicts, truncated evidence, delayed work after timeout, legitimate rejections, and correct retries. Each known defect must have a positive detection case and a nearby correct negative case.

The correct variants MUST be evaluated against the same invariants as broken variants. Do not suppress checks, loosen thresholds, or special-case variant names. Every unexpected failure on a correct variant is investigated against raw PostgreSQL records before release. Performance/latency observations are informational in v1 and never redefine correctness.
