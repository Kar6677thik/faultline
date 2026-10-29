import { useState } from "react";
import type { RunSummary } from "../api";
import { Badge, Link, Loading, StateMessage, useResource } from "./common";

const FILTERS = ["", "PASS", "FAIL", "INCONCLUSIVE", "ERROR", "RUNNING", "INTERRUPTED"];

export function RunList() {
  const [verdict, setVerdict] = useState("");
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1];
  const query = new URLSearchParams({ limit: "50" });
  if (verdict) query.set("verdict", verdict);
  if (cursor) query.set("cursor", cursor);
  const [resource, reload] = useResource<RunSummary[]>(`/api/v1/runs?${query}`);

  return (
    <section aria-labelledby="runs-title">
      <div className="toolbar">
        <h2 id="runs-title">Runs</h2>
        <label>
          Verdict{" "}
          <select
            value={verdict}
            onChange={(event) => {
              setVerdict(event.target.value);
              setCursors([]);
            }}
          >
            {FILTERS.map((value) => (
              <option key={value} value={value}>
                {value || "All"}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={reload}>
          Refresh
        </button>
      </div>
      {resource.status === "loading" ? <Loading label="runs" /> : null}
      {resource.status === "error" ? <StateMessage error={resource.error} /> : null}
      {resource.status === "ready" ? (
        <RunTable
          runs={resource.value.data}
          next={resource.value.meta.next_cursor as string | null | undefined}
          hasPrevious={cursors.length > 0}
          onNext={(next) => setCursors([...cursors, next])}
          onPrevious={() => setCursors(cursors.slice(0, -1))}
        />
      ) : null}
    </section>
  );
}

function RunTable({
  runs,
  next,
  hasPrevious,
  onNext,
  onPrevious,
}: {
  runs: RunSummary[];
  next?: string | null;
  hasPrevious: boolean;
  onNext: (cursor: string) => void;
  onPrevious: () => void;
}) {
  if (runs.length === 0) {
    return (
      <p className="empty" data-state="empty">
        No runs match. Create one with <code>faultline run</code>; this viewer is read-only.
      </p>
    );
  }
  return (
    <>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Scenario</th>
              <th scope="col">Variant</th>
              <th scope="col">Created (UTC)</th>
              <th scope="col">Outcome</th>
              <th scope="col">Required faults</th>
              <th scope="col">Lineage</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td>
                  <Link to={`/runs/${run.run_id}`}>
                    <code>{run.run_id.slice(0, 8)}</code>
                    <span className="sr-only"> run {run.run_id}</span>
                  </Link>
                </td>
                <td>{run.scenario_id}</td>
                <td>{run.variant}</td>
                <td>
                  <time dateTime={run.created_at}>{run.created_at.replace("T", " ").slice(0, 19)}</time>
                </td>
                <td>
                  <Badge value={run.verdict} />
                </td>
                <td>
                  {run.coverage
                    ? `${run.coverage.required_applied}/${run.coverage.required_total}`
                    : "—"}
                </td>
                <td>
                  <Lineage run={run} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <nav className="pager" aria-label="Run pages">
        <button type="button" onClick={onPrevious} disabled={!hasPrevious}>
          Newer
        </button>
        <button type="button" onClick={() => next && onNext(next)} disabled={!next}>
          Older
        </button>
      </nav>
    </>
  );
}

export function Lineage({ run }: { run: Pick<RunSummary, "origin" | "source_run_id" | "group_id"> }) {
  if (!run.origin || run.origin === "run") {
    return run.group_id ? <Link to={`/groups/${run.group_id}`}>repeat group</Link> : <span>new run</span>;
  }
  return (
    <span>
      {run.origin}
      {run.source_run_id ? (
        <>
          {" of "}
          <Link to={`/runs/${run.source_run_id}`}>
            <code>{run.source_run_id.slice(0, 8)}</code>
          </Link>
        </>
      ) : null}
      {run.group_id ? (
        <>
          {" · "}
          <Link to={`/groups/${run.group_id}`}>group</Link>
        </>
      ) : null}
    </span>
  );
}
