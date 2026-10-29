import type { RunDetail, SnapshotPage } from "../api";
import { Badge, Link, Loading, Money, StateMessage, useResource, Value } from "./common";

/** "snapshots/final.json#/records/ledger" -> { snapshot: "final", recordSet: "ledger" } */
export function parseEvidence(reference: string): { snapshot: string; recordSet?: string } | null {
  const match = /^snapshots\/([a-z][a-z0-9_-]{0,63})\.json(?:#\/records(?:\/([a-z_]{1,64}))?)?$/.exec(
    reference,
  );
  return match ? { snapshot: match[1], recordSet: match[2] } : null;
}

export function InvariantView({ runId, index }: { runId: string; index: number }) {
  const [resource] = useResource<RunDetail>(`/api/v1/runs/${runId}`);
  if (resource.status === "loading") return <Loading label="invariant" />;
  if (resource.status === "error") return <StateMessage error={resource.error} />;
  const run = resource.value.data;
  const finding = run.report?.invariants[index];
  if (!finding) {
    return (
      <section role="alert" data-state="not_found">
        <h2>Invariant not found</h2>
        <Link to={`/runs/${runId}`}>Back to run</Link>
      </section>
    );
  }
  const references = new Map<string, Set<string>>();
  for (const reference of [...finding.evidence, ...finding.failures.flatMap((f) => f.evidence)]) {
    const parsed = parseEvidence(reference);
    if (!parsed?.recordSet) continue;
    if (!references.has(parsed.recordSet)) references.set(parsed.recordSet, new Set());
    references.get(parsed.recordSet)!.add(parsed.snapshot);
  }
  const operations = (run.plan?.scenario.operations ?? []).filter(
    (op) => op.logical_id === finding.logical_id,
  );
  const attempts = (run.report?.attempts ?? []).filter((attempt) =>
    operations.some((op) => attempt.attempt_id.startsWith(`${op.id}/`)),
  );
  return (
    <article aria-labelledby="invariant-title">
      <p>
        <Link to={`/runs/${runId}`}>← Run {runId.slice(0, 8)}</Link>
      </p>
      <h2 id="invariant-title">
        {finding.invariant_id} v{finding.version} for <code>{finding.logical_id}</code>
      </h2>
      <p>
        <Badge value={finding.status} /> in a run whose overall verdict is <Badge value={run.verdict} />
      </p>
      {finding.reason ? <p>Reason: {finding.reason}</p> : null}
      {finding.failures.map((failure, i) => (
        <section key={`${failure.code}-${i}`} aria-label={`Violation ${failure.code}`}>
          <h3>
            Violation <code>{failure.code}</code>
          </h3>
          <dl className="failure">
            <dt>Expected</dt>
            <dd>
              <Value value={failure.expected} />
            </dd>
            <dt>Observed</dt>
            <dd>
              <Value value={failure.observed} />
            </dd>
            <dt>Evidence</dt>
            <dd>{failure.evidence.join(", ")}</dd>
          </dl>
        </section>
      ))}
      <section aria-labelledby="requests-title">
        <h3 id="requests-title">Requests for this logical identity</h3>
        <ul>
          {attempts.length === 0 ? <li>No attempts were dispatched.</li> : null}
          {attempts.map((attempt) => (
            <li key={attempt.attempt_id}>
              <code>{attempt.attempt_id}</code>: upstream{" "}
              {attempt.upstream
                ? `${attempt.upstream.outcome}${attempt.upstream.status ? ` ${attempt.upstream.status}` : ""}`
                : "none"}
              ; client saw <strong>{attempt.outcome}</strong>
              {attempt.cause ? ` (${String(attempt.cause)})` : ""}
            </li>
          ))}
        </ul>
      </section>
      {[...references.entries()].map(([recordSet, snapshots]) => (
        <StateComparison
          key={recordSet}
          runId={runId}
          recordSet={recordSet}
          snapshots={[...new Set(["baseline", ...snapshots])].filter((s) =>
            run.snapshots.includes(s),
          )}
          logicalId={finding.logical_id}
        />
      ))}
    </article>
  );
}

function StateComparison({
  runId,
  recordSet,
  snapshots,
  logicalId,
}: {
  runId: string;
  recordSet: string;
  snapshots: string[];
  logicalId: string;
}) {
  return (
    <section aria-labelledby={`state-${recordSet}`}>
      <h3 id={`state-${recordSet}`}>
        <code>{recordSet}</code>: {snapshots.join(" → ")}
      </h3>
      <div className="comparison">
        {snapshots.map((snapshot) => (
          <SnapshotTable
            key={snapshot}
            runId={runId}
            snapshot={snapshot}
            recordSet={recordSet}
            logicalId={logicalId}
          />
        ))}
      </div>
    </section>
  );
}

export function SnapshotTable({
  runId,
  snapshot,
  recordSet,
  logicalId,
}: {
  runId: string;
  snapshot: string;
  recordSet: string;
  logicalId?: string;
}) {
  const [resource] = useResource<SnapshotPage>(
    `/api/v1/runs/${runId}/snapshots/${snapshot}?record_set=${recordSet}&offset=0&limit=200`,
  );
  if (resource.status === "loading") return <Loading label={`${snapshot} ${recordSet}`} />;
  if (resource.status === "error") return <StateMessage error={resource.error} />;
  const page = resource.value.data;
  const columns = page.rows.length ? Object.keys(page.rows[0]) : [];
  return (
    <figure className="snapshot">
      <figcaption>
        {snapshot} ({page.total} row{page.total === 1 ? "" : "s"}
        {page.complete ? "" : ", INCOMPLETE"}
        {page.quiescent ? "" : ", not quiescent"})
        {page.total > page.rows.length ? ` · first ${page.rows.length} shown` : ""}
      </figcaption>
      {page.rows.length === 0 ? (
        <p className="empty">No records.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th scope="col" key={column}>
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.rows.map((row, i) => {
                const related = logicalId !== undefined && row.purchase_id === logicalId;
                return (
                  <tr key={i} className={related ? "related" : undefined}>
                    {columns.map((column) => (
                      <td key={column}>
                        {column.endsWith("_minor") ? <Money minor={row[column]} /> : <Value value={row[column]} />}
                        {related && column === columns[0] ? (
                          <span className="sr-only"> (this logical identity)</span>
                        ) : null}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </figure>
  );
}
