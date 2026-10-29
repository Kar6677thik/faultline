import { useEffect, useState } from "react";
import { ApiError, type EventPage, getJson, type HistoryEvent } from "../api";
import { Loading, StateMessage } from "./common";

const PAGE = 200;
// Bounded in-DOM history: older pages are dropped instead of rendering everything.
const MAX_RENDERED = 2000;

const KIND: Record<string, string> = {
  upstream_response_complete: "observed · upstream",
  client_outcome: "observed · client",
  attempt_dispatched: "observed · dispatch",
  attempt_not_needed: "plan · not needed",
  retry_eligible: "plan · retry rule",
  fault_eligible: "plan · fault trigger",
  fault_requested: "plan · fault",
  fault_applied: "observed · fault acknowledged",
  fault_healed: "observed · fault healed",
  fault_unapplied: "observed · fault not applied",
  lifecycle: "lifecycle",
  snapshot_captured: "observation boundary",
  baseline_captured: "observation boundary",
  quiescence: "observation boundary",
};

export function Timeline({ runId, live }: { runId: string; live: boolean }) {
  const [events, setEvents] = useState<HistoryEvent[]>([]);
  const [after, setAfter] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<string[]>([]);

  const load = async (from: number) => {
    setBusy(true);
    try {
      const { data, meta } = await getJson<EventPage>(
        `/api/v1/runs/${runId}/events?after_sequence=${from}&limit=${PAGE}`,
      );
      setEvents((current) => [...current, ...data.events].slice(-MAX_RENDERED));
      setAfter(data.next_after_sequence);
      setHasMore(data.has_more);
      setNotes((meta.diagnostics as string[] | undefined) ?? []);
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError("error", String(caught)));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    setEvents([]);
    setAfter(0);
    load(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (!live || hasMore) return;
    const timer = setInterval(() => load(after), 2000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, hasMore, after]);

  return (
    <section aria-labelledby="timeline-title">
      <h3 id="timeline-title">Timeline (durable sequence order)</h3>
      <p className="muted">
        Ordered by the coordinator's durable sequence. Wall-clock times come from different clocks and
        are not proof of causality; follow the caused-by references instead.
      </p>
      {notes.map((note) => (
        <p key={note} role="note" data-state="incomplete">
          {note}
        </p>
      ))}
      {error ? <StateMessage error={error} /> : null}
      {events.length === 0 && busy ? <Loading label="events" /> : null}
      <ol className="timeline">
        {events.map((event) => (
          <li key={event.event_id} id={`event-${event.sequence}`}>
            <details>
              <summary>
                <span className="seq">#{event.sequence}</span> <code>{event.type}</code>{" "}
                <span className="kind">{KIND[event.type] ?? event.source}</span>
                {event.attempt_id ? (
                  <>
                    {" · attempt "}
                    <code>{event.attempt_id}</code>
                  </>
                ) : event.operation_id ? (
                  <>
                    {" · op "}
                    <code>{event.operation_id}</code>
                  </>
                ) : null}
                {event.fault_id ? (
                  <>
                    {" · fault "}
                    <code>{event.fault_id}</code>
                  </>
                ) : null}
              </summary>
              <dl className="facts">
                <dt>Source / monotonic ns</dt>
                <dd>
                  {event.source} / <code>{event.source_monotonic_ns}</code>
                </dd>
                <dt>Wall time (not causal)</dt>
                <dd>{event.wall_time}</dd>
                {event.caused_by_event_ids?.length ? (
                  <>
                    <dt>Caused by</dt>
                    <dd>{event.caused_by_event_ids.join(", ")}</dd>
                  </>
                ) : null}
                <dt>Payload</dt>
                <dd>
                  <code className="json">{JSON.stringify(event.payload, null, 1)}</code>
                </dd>
              </dl>
            </details>
          </li>
        ))}
      </ol>
      {hasMore ? (
        <button type="button" onClick={() => load(after)} disabled={busy}>
          {busy ? "Loading…" : `Load next ${PAGE} events`}
        </button>
      ) : events.length > 0 ? (
        <p className="muted">End of recorded history{live ? " so far" : ""}.</p>
      ) : null}
    </section>
  );
}
