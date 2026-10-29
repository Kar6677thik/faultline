# The read-only evidence viewer and local API

Think of a museum display case. Visitors can look at everything inside, read the
labels, and follow the story from one exhibit to the next. They cannot open the case,
rearrange the objects, or bring in their own. Only someone holding the key, which is
handed out at the door, can even enter the room.

`faultline serve` builds that display case around your evidence bundles:

- it **only reads**. It never starts experiments, stops containers, imports adapter
  code, or edits an artifact;
- it listens **only on 127.0.0.1**. There is no option to listen on another address;
- every API request must carry a **fresh random token** created when the server starts;
- the browser page is **packaged with Faultline**. It loads nothing from the internet
  and contains no run data until it asks the API.

## Start it

```bash
faultline serve              # http://127.0.0.1:8765/#token=...
faultline serve --port 0     # pick a free port and print it
faultline serve --open       # also open the printed URL in a browser
```

The printed launch URL carries the token in the part after `#`, the *fragment*.
Browsers never send the fragment to a server, so the token does not appear in server
logs or `Referer` headers. The page reads it once, keeps it in memory, and removes it
from the address bar. If you reload the page, the token is gone on purpose; reopen the
URL printed in your terminal. Faultline does not weaken authentication to make reloads
convenient.

## What protects the API

The checks live in the `boundary` middleware in
[`server.py`](../../src/faultline/server.py). Each one blocks a specific attack:

| Check | Rejects | Why it matters |
| --- | --- | --- |
| Bind to 127.0.0.1 only | other machines | evidence may contain business data |
| `Host` must be `127.0.0.1:<port>` or `localhost:<port>` | 403 | a hostile page could otherwise use DNS rebinding to reach the server through another name |
| `Origin`, when present, must be this server | 403 | another tab's scripts cannot read the API |
| `Authorization: Bearer <token>` on `/api/*` | 401 | another local program cannot read evidence without the launch URL |
| only `GET`/`HEAD` | 405 | nothing can mutate through the API |
| no CORS headers at all | — | browsers keep cross-origin reads blocked |
| strict Content-Security-Policy, `nosniff`, `no-referrer`, frame denial | — | even malicious text inside an artifact cannot run as script or be framed |

Requests never name a file path. Runs and groups are addressed by canonical UUIDs.
Snapshots must appear in the bundle's inventory, and record sets must exist in that
snapshot. Downloads use an opaque artifact ID from the inventory; the bytes are
re-hashed before sending and served as `attachment` with a plain-text or JSON type.
`execution_root`, an absolute local path, is removed from the manifest the API returns.

## What the API returns

Every response is `{data, meta}`, and every error is
`{error: {code, message, details}}`:

| Status | Meaning |
| --- | --- |
| 400 | malformed query (limit out of range, bad cursor) |
| 401 | missing or wrong token |
| 403 | wrong Host or Origin |
| 404 | unknown ID, or `artifact_not_ready` for a file not written yet |
| 405 | any mutation method |
| 409 | a known artifact with an unsupported version |
| 422 | corrupt evidence (hash mismatch, invalid history) |
| 304 | the ETag matches |

JavaScript numbers lose precision above 2^53. So the API turns money (`*_minor`),
nanosecond timestamps (`*_ns`), and any unsafe integer into **decimal strings**. Counts
and bounded milliseconds stay numbers. Artifacts on disk keep real integers; only the
API presentation changes (`presentable()`).

Run listing is newest-first by creation time and then run ID. The cursor remembers the
first page's time boundary and the last row seen, so runs created while you page do not
shift later pages. Unfinished bundles have no report, so they are labelled `RUNNING`
(written to within the last two minutes) or `INTERRUPTED`; they are never shown as
complete. The server deliberately does not touch the execution lock to decide this:
taking a lock just to look could make a starting run fail.

## Integrity rules the reader enforces

`read_bundle` in [`artifacts.py`](../../src/faultline/artifacts.py) is used by
`report`, replay, reduction, and the API. Besides checking inventory hashes and safe
paths, it rejects:

- truncated last history lines, unreadable JSON, and non-contiguous sequences;
- duplicate event IDs, even when someone rewrote the hashes consistently;
- a report that relies on an attempt without a recorded `client_outcome` event;
- unsupported bundle, report, or event versions. These raise
  `UnsupportedArtifact`, which the API maps to 409 rather than 422, because the data
  may be fine and simply newer than this reader.

Hashes detect accidental damage, not a determined forger who rewrites everything. The
semantic checks catch some consistent rewrites, but this is still not authenticity.

## The viewer

The React app lives in [`web/`](../../web) and is built into
`src/faultline/viewer/`, so `pip install` users need no Node.js. The investigation path:

1. **Run list:** scenario, variant, time, verdict (text plus symbol, never colour
   alone), required-fault coverage, and lineage (replay of / comparison of / reduction
   trial, with a group link). The verdict filter is applied by the server, so filtering
   never loads every run.
2. **Run view:** outcome first. PASS states its limited scope. An ERROR shows its harness
   errors *and* any violations, explaining why they are not a trustworthy reproduction.
   Then come violated and unknown invariants with expected and observed values, coverage
   and cleanup, an **attempts table that joins the plan with the observation**,
   and faults marked acknowledged or not applied. The table shows planned attempts,
   upstream witness versus client outcome, and attempts that were never needed.
3. **Timeline:** durable sequence order, 200 events per page, at most 2000 kept in the
   page at once. It warns that wall-clock time is not causality.
4. **Invariant detail:** the requests for that logical identity, and baseline-versus-final
   tables for each record set the finding cites. Rows for the identity are highlighted,
   and money is shown exactly in paise and rupees.
5. **Group view:** every count, both reproduction denominators, comparison
   differences, and for reductions the original versus best objective, baseline and
   validation counts, budget, every candidate and trial, and a clear
   **verified/unverified** label. It never says "minimal".

Loading, empty, unauthorized, not found, not ready, corrupt, unsupported, incomplete,
and disconnected are all different messages. Active runs and groups poll every two
seconds and stop when final. Controls are native links and buttons, focus stays
visible, and status changes use `role="status"`/`role="alert"`. Layouts collapse to a
single column below 600 px and work down to 375 px wide.

## Tests

- [`tests/test_server.py`](../../tests/test_server.py) (AT-018, AT-020): token, Host,
  Origin, mutation methods, no CORS, strict headers, a data-free page, opaque artifact
  IDs, path traversal attempts, and artifacts changed after listing. It also covers
  integer presentation, stable pagination with a run added mid-listing, RUNNING and
  INTERRUPTED bundles with a truncated history line, event pagination and ETags,
  snapshot bounds, and 409 versus 422, plus sibling-root group access. The
  read-bundle checks cover round trip, duplicate IDs, missing attempt events, truncated
  lines, and future event versions.
- [`web/src/test/viewer.test.tsx`](../../web/src/test/viewer.test.tsx) (AT-019):
  - token capture and fragment removal, and the reload instruction;
  - each distinct state, and an incomplete bundle that never looks complete;
  - upstream versus client outcome and planned versus observed attempts;
  - ERROR plus violation, PASS scope, exact 64-bit values, and markup in artifacts
    shown as text;
  - group denominators, an unverified reduction, and a full keyboard path from the
    run list to the duplicate ledger rows.

Run the viewer tests with `cd web && npm ci && npm test`, and rebuild the packaged
assets with `npm run build`.

## Limits

- Anyone who can read the terminal output or the process's memory can use the token.
  This protects against other websites and casual local access, not against your own
  user account.
- RUNNING versus INTERRUPTED is a recency heuristic, not proof of a live process.
- The server reads whole history files to page through them. That is fine for the
  50 MiB artifact ceiling, but it is not an indexed store.
- The viewer has been tested in jsdom and against the real API with curl. A manual
  browser walkthrough is not recorded as acceptance evidence here.
