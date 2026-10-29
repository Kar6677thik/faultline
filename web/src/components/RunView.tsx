import { useState } from "react";
import { type Attempt, downloadArtifact, type Finding, type RunDetail } from "../api";
import { Lineage } from "./RunList";
import { Timeline } from "./Timeline";
import { Badge, Link, Loading, StateMessage, useResource, Value } from "./common";

const ACTIVE = new Set(["RUNNING"]);

export function RunView({ runId }: { runId: string }) {
  const [resource] = useResource<RunDetail>(`/api/v1/runs/${runId}`, {
    pollMs: 2000,
    active: (run) => ACTIVE.has(run.verdict),
  });
  if (resource.status === "loading") return <Loading label="run" />;
  if (resource.status === "error") return <StateMessage error={resource.error} />;
  const run = resource.value.data;
  return <RunBody run={run} />;
}

export function RunBody({ run }: { run: RunDetail }) {
  const report = run.report;
  const findings = report?.invariants ?? [];
  const violated = findings.filter((finding) => finding.status === "FAIL");
  const unknown = findings.filter((finding) => finding.status === "UNKNOWN");
  const passed = findings.filter((finding) => finding.status === "PASS");
  const errors = (report?.diagnostics ?? []).filter((d) => d.severity === "error");
  return (
    <article aria-labelledby="run-title">
      <p>
        <Link to="/">← All runs</Link>
      </p>
      <header className="run-header">
        <h2 id="run-title">
          Run <code>{run.run_id}</code>
        </h2>
        <p className="outcome" aria-live="polite">
          <Badge value={run.verdict} />{" "}
          {run.completeness === "incomplete" ? (
            <strong data-state="incomplete">
              Incomplete bundle: no final report. Nothing below is a completed verdict.
            </strong>
          ) : null}
        </p>
        <dl className="facts">
          <dt>Scenario</dt>
          <dd>{run.manifest.scenario_id}</dd>
          <dt>Variant</dt>
          <dd>{run.manifest.variant}</dd>
          <dt>Created (UTC)</dt>
          <dd>{run.manifest.created_at}</dd>
          <dt>Lineage</dt>
          <dd>
            <Lineage run={{ origin: run.manifest.origin ?? "run", ...run.manifest }} />
          </dd>
          <dt>Plan hash</dt>
          <dd>
            <code className="hash">{run.manifest.plan_hash}</code>
          </dd>
        </dl>
      </header>

      {run.verdict === "PASS" ? (
        <p className="scope" role="note">
          PASS means only the properties listed below held for this recorded workload and these
          faults. It is not proof of correctness under other inputs or schedules.
        </p>
      ) : null}

      {run.manifest.comparison ? (
        <section aria-labelledby="comparison-title">
          <h3 id="comparison-title">Comparison, not replay</h3>
          <p>This run used the source plan against a different environment:</p>
          <Differences items={run.manifest.comparison.differences} />
        </section>
      ) : null}

      {errors.length > 0 ? (
        <section aria-labelledby="errors-title" className="harness-errors">
          <h3 id="errors-title">Harness errors ({errors.length})</h3>
          <p>
            The harness itself failed, so this run is ERROR. Any violations below are still shown, but
            they are not a trustworthy reproduction.
          </p>
          <ul>
            {errors.map((d, i) => (
              <li key={`${d.code}-${i}`}>
                <code>{d.code}</code>: {d.message}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {report ? (
        <>
          <section aria-labelledby="violations-title">
            <h3 id="violations-title">
              Violated invariants ({violated.length}) · Unknown ({unknown.length})
            </h3>
            {violated.length + unknown.length === 0 ? <p>None.</p> : null}
            <FindingList run={run} findings={[...violated, ...unknown]} all={findings} />
          </section>
          <section aria-labelledby="coverage-title">
            <h3 id="coverage-title">Coverage and cleanup</h3>
            <ul>
              <li>
                Required faults acknowledged: {report.coverage.required_applied}/
                {report.coverage.required_total}
                {report.coverage.unapplied.length > 0
                  ? ` (not applied: ${report.coverage.unapplied.join(", ")})`
                  : ""}
              </li>
              <li>Workload complete: {report.coverage.workload_complete ? "yes" : "no"}</li>
              <li>
                Cleanup:{" "}
                {report.cleanup.complete ? (
                  "complete"
                ) : (
                  <strong>
                    INCOMPLETE: {report.cleanup.remaining_resources.length} resource(s) remain; run{" "}
                    <code>faultline clean --run {run.run_id}</code>
                  </strong>
                )}
              </li>
            </ul>
          </section>
          <Attempts run={run} />
          <details>
            <summary>Passing invariants ({passed.length})</summary>
            <FindingList run={run} findings={passed} all={findings} />
          </details>
        </>
      ) : null}

      <Timeline runId={run.run_id} live={run.verdict === "RUNNING"} />
      <Artifacts run={run} />
    </article>
  );
}

function FindingList({ run, findings, all }: { run: RunDetail; findings: Finding[]; all: Finding[] }) {
  return (
    <ul className="findings">
      {findings.map((finding) => {
        const index = all.indexOf(finding);
        return (
          <li key={`${finding.invariant_id}-${finding.logical_id}`}>
            <Badge value={finding.status} />{" "}
            <Link to={`/runs/${run.run_id}/invariants/${index}`}>
              {finding.invariant_id} v{finding.version}
            </Link>{" "}
            for <code>{finding.logical_id}</code>
            {finding.reason ? <span> · reason: {finding.reason}</span> : null}
            {finding.failures.map((failure, i) => (
              <dl className="failure" key={`${failure.code}-${i}`}>
                <dt>Violation</dt>
                <dd>
                  <code>{failure.code}</code>
                </dd>
                <dt>Expected</dt>
                <dd>
                  <Value value={failure.expected} />
                </dd>
                <dt>Observed</dt>
                <dd>
                  <Value value={failure.observed} />
                </dd>
              </dl>
            ))}
          </li>
        );
      })}
    </ul>
  );
}

function witnessText(attempt: Attempt) {
  const upstream = attempt.upstream;
  if (!upstream) return "no upstream response recorded";
  return upstream.status !== undefined ? `${upstream.outcome} ${upstream.status}` : upstream.outcome;
}

export function Attempts({ run }: { run: RunDetail }) {
  const report = run.report;
  if (!report) return null;
  const planned = run.plan?.scenario.operations ?? [];
  const observed = new Map(report.attempts.map((attempt) => [attempt.attempt_id, attempt]));
  const rows = planned.flatMap((op) =>
    Array.from({ length: op.retry.max_attempts }, (_, i) => ({ op, id: `${op.id}/${i + 1}` })),
  );
  return (
    <section aria-labelledby="attempts-title">
      <h3 id="attempts-title">Attempts: plan versus observation</h3>
      <p className="muted">
        “Upstream” is what the application actually returned; “client” is what the retry policy saw.
        A suppressed success appears as an upstream 201 with a client timeout.
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Attempt (planned)</th>
              <th scope="col">Logical ID</th>
              <th scope="col">Upstream witness</th>
              <th scope="col">Client outcome</th>
              <th scope="col">Cause</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ op, id }) => {
              const attempt = observed.get(id);
              return (
                <tr key={id} id={`attempt-${id}`}>
                  <td>
                    <code>{id}</code>
                  </td>
                  <td>
                    <code>{op.logical_id}</code>
                  </td>
                  <td>{attempt ? witnessText(attempt) : "—"}</td>
                  <td>{attempt ? attempt.outcome : <em>not dispatched / not needed</em>}</td>
                  <td>{attempt?.cause ? String(attempt.cause) : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <h4>Faults</h4>
      <ul>
        {(run.plan?.scenario.faults ?? []).map((fault) => (
          <li key={fault.id}>
            <code>{fault.id}</code> ({fault.kind}
            {fault.required ? ", required" : ""}):{" "}
            {report.coverage.applied.includes(fault.id) ? (
              <strong>acknowledged as applied</strong>
            ) : (
              <strong>not applied</strong>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Differences({ items }: { items: { field: string; source: unknown; current: unknown }[] }) {
  if (items.length === 0) return <p>No recorded differences.</p>;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Source run</th>
            <th scope="col">This run</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.field}>
              <th scope="row">
                <code>{item.field}</code>
              </th>
              <td>
                <Value value={item.source} />
              </td>
              <td>
                <Value value={item.current} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Artifacts({ run }: { run: RunDetail }) {
  const [failed, setFailed] = useState<string | null>(null);
  return (
    <section aria-labelledby="artifacts-title">
      <h3 id="artifacts-title">Artifacts</h3>
      {failed ? <p role="alert">Download failed: {failed}</p> : null}
      <ul>
        {run.artifacts.map((artifact) => (
          <li key={artifact.artifact_id}>
            <button
              type="button"
              onClick={() =>
                downloadArtifact(run.run_id, artifact.artifact_id, artifact.name).catch((error) =>
                  setFailed(String(error.message ?? error)),
                )
              }
            >
              Download <code>{artifact.name}</code>
            </button>{" "}
            <span className="muted">
              {artifact.bytes} bytes · sha256 <code className="hash">{artifact.sha256.slice(0, 12)}</code>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
