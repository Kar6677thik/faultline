import type { GroupRecord } from "../api";
import { Differences } from "./RunView";
import { Badge, Link, Loading, StateMessage, useResource } from "./common";

const OUTCOME_TEXT: Record<string, string> = {
  reduction_verified: "Smaller reproduction found and verified by five fresh runs",
  reduction_unverified: "Smaller candidate found but NOT verified",
  no_smaller_reproduction: "No smaller reproduction found within the search",
  baseline_unstable: "Original failure was not stable enough to reduce",
  insufficient_budget: "Budget ended before a meaningful result",
};

export function GroupView({ groupId }: { groupId: string }) {
  const [resource] = useResource<GroupRecord>(`/api/v1/groups/${groupId}`, {
    pollMs: 2000,
    active: (group) => group.status === "running",
  });
  if (resource.status === "loading") return <Loading label="group" />;
  if (resource.status === "error") return <StateMessage error={resource.error} />;
  const group = resource.value.data;
  return (
    <article aria-labelledby="group-title">
      <p>
        <Link to="/">← All runs</Link>
      </p>
      <h2 id="group-title">
        {group.kind.replace("_", " ")} group <code>{group.group_id}</code>
      </h2>
      <p aria-live="polite">
        Status: <strong>{group.status}</strong>
        {group.source_run_id ? (
          <>
            {" · source "}
            <Link to={`/runs/${group.source_run_id}`}>
              <code>{group.source_run_id.slice(0, 8)}</code>
            </Link>
          </>
        ) : null}
      </p>
      {group.kind === "reduction" ? <Reduction group={group} /> : <Repeats group={group} />}
    </article>
  );
}

export function Repeats({ group }: { group: GroupRecord }) {
  const counts = group.counts ?? {};
  return (
    <>
      {group.kind === "comparison" ? (
        <section aria-labelledby="diff-title">
          <h3 id="diff-title">Comparison, not replay equivalence</h3>
          <p>
            Same plan, different environment: {group.source_variant} → {group.variant}.
          </p>
          <Differences items={group.differences ?? []} />
        </section>
      ) : null}
      <section aria-labelledby="counts-title">
        <h3 id="counts-title">Every trial counted</h3>
        <dl className="facts counts">
          <dt>Scheduled</dt>
          <dd>{group.scheduled}</dd>
          <dt>Started</dt>
          <dd>{group.started}</dd>
          <dt>Completed</dt>
          <dd>{group.completed}</dd>
          <dt>Interrupted</dt>
          <dd>{group.interrupted}</dd>
          <dt>Unstarted</dt>
          <dd>{group.unstarted}</dd>
          {["PASS", "FAIL", "INCONCLUSIVE", "ERROR"].map((verdict) => (
            <div key={verdict} className="pair">
              <dt>{verdict}</dt>
              <dd>{counts[verdict] ?? 0}</dd>
            </div>
          ))}
        </dl>
        {group.target_signature ? (
          <>
            <p>
              Target signature <code className="hash">{group.target_signature}</code>: matching
              failures {group.matching_failures}, other failures {group.other_failures}.
            </p>
            <ul>
              <li>
                Per started trial: <strong>{group.reproduction?.per_started.ratio}</strong>
              </li>
              <li>
                Per conclusive trial (completed PASS or FAIL):{" "}
                <strong>{group.reproduction?.per_conclusive.ratio}</strong>
              </li>
            </ul>
            <p className="muted">
              ERROR and interrupted trials never count as reproductions. These are observations of
              these trials, not a probability for all schedules.
            </p>
          </>
        ) : (
          <p>No target signature, so no reproduction fraction is claimed.</p>
        )}
      </section>
      <Trials trials={group.trials ?? []} />
    </>
  );
}

function Trials({
  trials,
}: {
  trials: { run_id: string; verdict: string; matches_target: boolean }[];
}) {
  return (
    <section aria-labelledby="trials-title">
      <h3 id="trials-title">Trials ({trials.length})</h3>
      <ol>
        {trials.map((trial) => (
          <li key={trial.run_id}>
            <Link to={`/runs/${trial.run_id}`}>
              <code>{trial.run_id.slice(0, 8)}</code>
            </Link>{" "}
            <Badge value={trial.verdict} /> {trial.matches_target ? "matches target" : "does not match"}
          </li>
        ))}
      </ol>
    </section>
  );
}

export function Reduction({ group }: { group: GroupRecord }) {
  const outcome = group.outcome ?? "running";
  const verified = outcome === "reduction_verified";
  const validation = group.validation;
  return (
    <>
      <section aria-labelledby="outcome-title">
        <h3 id="outcome-title">Outcome</h3>
        <p data-outcome={outcome}>
          <strong>{OUTCOME_TEXT[outcome] ?? outcome}</strong>
          {group.termination_reason ? ` · stopped: ${group.termination_reason}` : ""}
        </p>
        {!verified && group.best ? (
          <p role="note">
            The best candidate below passed its search trials only. It is not a verified reduced
            reproduction.
          </p>
        ) : null}
        {group.target ? (
          <p>
            Target: <code>{group.target.invariant_id}</code> / <code>{group.target.code}</code> for{" "}
            <code>{group.target.logical_id}</code>
          </p>
        ) : null}
      </section>
      <section aria-labelledby="objective-title">
        <h3 id="objective-title">Objective (operations, faults, potential attempts)</h3>
        <dl className="facts">
          <dt>Original</dt>
          <dd>{group.original_objective?.join(", ")}</dd>
          <dt>Best candidate</dt>
          <dd>{group.best ? group.best.objective.join(", ") : "none accepted"}</dd>
          {group.best ? (
            <>
              <dt>Removed operations</dt>
              <dd>{group.best.removed_operations.join(", ") || "none"}</dd>
              <dt>Removed faults</dt>
              <dd>{group.best.removed_faults.join(", ") || "none"}</dd>
            </>
          ) : null}
        </dl>
        <p className="muted">“Smaller”, not “minimal”: the search is greedy and budgeted.</p>
      </section>
      <section aria-labelledby="series-title">
        <h3 id="series-title">Baseline and final validation</h3>
        <ul>
          <li>
            Baseline:{" "}
            {group.baseline
              ? `${group.baseline.matching}/${group.baseline.scheduled} matching, ${group.baseline.errors} ERROR`
              : "not run"}
          </li>
          <li>
            Final validation:{" "}
            {validation && "matching" in validation
              ? `${validation.matching}/${validation.scheduled} matching, ${validation.errors} ERROR`
              : validation
                ? "not completed (budget)"
                : "not run"}
          </li>
        </ul>
        {group.budget ? (
          <p>
            Budget: {group.budget.runs_used}/{group.budget.max_runs} trials,{" "}
            {group.budget.elapsed_ms}/{group.budget.max_duration_ms} ms.
          </p>
        ) : null}
      </section>
      <section aria-labelledby="candidates-title">
        <h3 id="candidates-title">Candidates ({group.candidates?.length ?? 0})</h3>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Phase</th>
                <th scope="col">Removed</th>
                <th scope="col">Objective</th>
                <th scope="col">Trials</th>
                <th scope="col">Decision</th>
              </tr>
            </thead>
            <tbody>
              {(group.candidates ?? []).map((candidate) => (
                <tr key={candidate.index}>
                  <td>{candidate.index}</td>
                  <td>{candidate.phase}</td>
                  <td>
                    <code>{JSON.stringify(candidate.removed)}</code>
                  </td>
                  <td>{candidate.objective.join(", ")}</td>
                  <td>
                    {candidate.trials.map((trial) => (
                      <Link key={trial.run_id} to={`/runs/${trial.run_id}`}>
                        <Badge value={trial.verdict} />
                        <span className="sr-only">
                          {trial.matches_target ? " matching" : " not matching"}
                        </span>{" "}
                      </Link>
                    ))}
                  </td>
                  <td>{candidate.accepted ? "kept" : `rejected (${candidate.reason})`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
