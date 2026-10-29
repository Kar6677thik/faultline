# Learn Faultline from the beginning

Faultline is a practice laboratory for backend reliability. Think of the application as a shop, the database as its notebook, and Faultline as someone checking what happens when the shop loses a receipt and a customer asks again.

The first question is small: **can one purchase accidentally charge the customer twice?** The tool sends a real request, hides a successful response from the retry policy, allows another attempt, and checks the database. The application and database actually run; the failure is not a fabricated log line.

This guide describes the code currently implemented. The [release specifications](../../specs/README.md) describe a larger destination. [Implementation status](../implementation-status.md) tells you which parts are still missing.

## Suggested reading order

1. [Walk through one purchase](walkthrough.md): the complete journey, each Python module, the database records, and what the checks mean.
2. [Understand the decisions](decisions.md): why we chose this design, the alternatives, and what we give up.
3. [Learn crash recovery](recovery.md): locks, ownership, hard kills, cleanup receipts, and the actual recovery procedure.
   Continue with [lost creation receipts](creation-recovery.md): write intent before creating equipment, bind its ID before deletion, and distinguish absence from uncertainty.
4. [Learn time budgets](deadlines.md): request timeouts, stage deadlines, reserved cleanup time, and what happens when work runs late.
5. [Understand adapters](adapters.md): separate the supervisor from the domain assistant, check the judge's outputs, and identify its source.
6. [Configure a project](projects.md): separate a recipe from its laboratory, constrain paths and budgets, and pin local images.
7. [Install an adapter](installed-adapters.md): package address cards, version checks, content fingerprints, and preflight deadlines; continue with [external applications](external-adapters.md) for the current public contract.
8. [Look up unfamiliar words](glossary.md): short explanations with Faultline examples.
9. [Run the README demo](../../README.md#demonstrate-the-defect-and-correction), then compare its evidence with [measured results](../acceptance-results.md).

## How to read the evidence

Start with `report.md` to understand the finding. Then look at `snapshots/final.json` to see the actual records. Use `history.ndjson` to understand the sequence that led there. The report's evidence paths connect a finding to those records.

For the standard purchase, `200000` means 200,000 paise, or INR 2,000. The initial wallet is 1,000,000 paise, or INR 10,000. A correct purchase leaves 800,000 paise. Two debits leave 600,000 paise. Every amount is an integer because floating-point approximations are unnecessary for this example.

A green result means the declared checks passed for this run with sufficient evidence. It does not mean the application is correct for all requests, timings, or failures.

## How the documentation will grow

For every material feature or change, document:

- The problem, with a small concrete example.
- The simple explanation first, followed by the precise contract.
- The source files responsible for the behavior.
- The decision, alternatives considered, tradeoffs, and remaining limitations.
- The tests and observations that support the claim.
- Commands a learner can run and the results they should expect.

Comments inside code explain local subtleties. These guides explain the larger relationships. Historical measurements stay in acceptance documents; changing an explanation must not turn an earlier measurement into a claim about new code.

## Small learning exercises

Before running the unsafe scenario, predict the number of orders, ledger entries, and inventory movements. Then check those predictions against the snapshot.

Ask why a wallet conservation check can pass even when the customer was charged twice. The answer is that the ledger and balance can agree about two incorrect charges. We need an identity check as well as an arithmetic check.

Compare a timeout with a rollback. A timeout describes what the client knows; a rollback describes what the database did. They are different facts.

Read the hard-kill integration test after the recovery guide. Identify which assertion proves the other environment survived, which proves the original files were preserved, and which prevents cleanup from inventing a successful experiment.

## Experiment engine and external applications

Read [the experiment engine walkthrough](experiment-engine.md) for generators, concurrent admission, barriers, retries, proxy faults, and in-flight evidence. Then read [external adapters](external-adapters.md) to run a separately packaged counter application and understand the public hooks. These extend the historical M1 walkthroughs; the implementation-status page remains the current scope reference.

The [M2 acceptance report](../m2-acceptance-results.md) connects each milestone gate
to actual tests and measurements, including failed development trials and their fixes.
Decisions L-037 through L-045 explain the engine's design and limits in plain language.

## Replay, reduction, and investigation (M3)

Read [replay, comparison, and honest repeat counts](replay.md) to learn why a strict
replay refuses changed images or code, how a comparison records its differences, and
how repeat groups count reproductions without hiding errors. Decisions L-046 through
L-048 record the tradeoffs.

Then read [reduction](reduction.md) to see how a 20-operation failure is shrunk by
deleting whole operations, faults, and retries. You'll see why every kept deletion needs
three matching fresh runs, and why running out of budget is reported differently from
finding nothing. Decisions L-049 through L-051 cover the additional fixture, the search
algorithm, and budgets.

Finally, read [the evidence viewer and local API](viewer.md) to see why the server is
read-only and loopback-only, how its token is handed out, and how the React viewer
leads from a run to the exact ledger rows. Decisions L-052 through L-054 explain the
web boundary, integrity checks, and packaging.
