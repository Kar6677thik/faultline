import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { captureToken, type GroupRecord, type RunDetail, setToken } from "../api";
import { App } from "../App";
import { Reduction, Repeats } from "../components/GroupView";
import { parseEvidence } from "../components/InvariantView";
import { RunBody } from "../components/RunView";
import { Money } from "../components/common";
import { navigate } from "../router";

const RUN = "11111111-2222-4333-8444-555555555555";
const SOURCE = "99999999-2222-4333-8444-555555555555";
const TOKEN = "t".repeat(43);

type Handler = (url: string) => { status: number; body: unknown };

function serve(handler: Handler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const headers = new Headers(init?.headers);
    expect(headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
    expect(url).not.toContain(TOKEN); // never in a query string
    const { status, body } = handler(url);
    return new Response(JSON.stringify(body), { status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function detail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: RUN,
    completeness: "complete",
    verdict: "FAIL",
    manifest: {
      scenario_id: "lost_response_purchase",
      variant: "unsafe",
      created_at: "2026-10-29T10:00:00+00:00",
      origin: "replay",
      source_run_id: SOURCE,
      plan_hash: "a".repeat(64),
      stage: "completed",
    },
    report: {
      verdict: "FAIL",
      lifecycle: "completed",
      invariants: [
        {
          invariant_id: "orders.at_most_one_debit",
          version: 1,
          status: "FAIL",
          logical_id: "purchase-a",
          failures: [
            {
              code: "duplicate_debit",
              expected: { maximum_count: 1 },
              observed: { count: 2, total_minor: "400000" },
              evidence: ["snapshots/final.json#/records/ledger"],
            },
          ],
          evidence: ["snapshots/baseline.json", "snapshots/final.json"],
          signatures: ["e".repeat(64)],
        },
        {
          invariant_id: "orders.wallet_conservation",
          version: 1,
          status: "PASS",
          logical_id: "purchase-a",
          failures: [],
          evidence: ["snapshots/final.json"],
          signatures: [],
        },
      ],
      diagnostics: [],
      coverage: {
        required_applied: 1,
        required_total: 1,
        applied: ["hide_first_response"],
        unapplied: [],
        workload_complete: true,
      },
      attempts: [
        {
          attempt_id: "purchase_a/1",
          outcome: "timeout",
          upstream: { outcome: "http_response", status: 201 },
          cause: "injected_response_suppression",
        },
        { attempt_id: "purchase_a/2", outcome: "http_response", upstream: { outcome: "http_response", status: 201 } },
      ],
      cleanup: { complete: true, remaining_resources: [] },
    },
    plan: {
      scenario: {
        operations: [{ id: "purchase_a", logical_id: "purchase-a", retry: { max_attempts: 3 } }],
        faults: [{ id: "hide_first_response", kind: "response_suppress", required: true }],
      },
    },
    artifacts: [],
    snapshots: ["baseline", "final"],
    diagnostics: [],
    ...overrides,
  };
}

const EMPTY_EVENTS = { data: { events: [], next_after_sequence: 0, has_more: false }, meta: {} };

beforeEach(() => {
  setToken(TOKEN);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  setToken(null);
});

describe("launch token", () => {
  it("is captured from the fragment and removed from the address bar", () => {
    setToken(null);
    window.history.replaceState(null, "", `/runs/${RUN}#token=${TOKEN}`);
    expect(captureToken(window.location, window.history)).toBe(TOKEN);
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe(`/runs/${RUN}`);
  });

  it("a reload without the fragment explains how to regain access", () => {
    setToken(null);
    render(<App />);
    expect(screen.getByRole("alert")).toHaveTextContent("Reopen the launch URL");
  });
});

describe("distinct states", () => {
  it.each([
    [404, { code: "not_found", message: "unknown run" }, "not_found"],
    [409, { code: "unsupported_version", message: "bundle v9" }, "unsupported"],
    [422, { code: "corrupt_evidence", message: "hash mismatch" }, "corrupt"],
    [404, { code: "artifact_not_ready", message: "pending" }, "not_ready"],
    [500, { code: "internal", message: "boom" }, "error"],
  ])("HTTP %i renders its own state", async (status, error, state) => {
    serve(() => ({ status, body: { error } }));
    window.history.replaceState(null, "", `/runs/${RUN}`);
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("data-state", state);
  });

  it("an unreachable server is disconnected, not empty", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("network"))));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveAttribute("data-state", "disconnected");
  });

  it("loading and empty run lists are distinct", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await gate;
        return new Response(JSON.stringify({ data: [], meta: { next_cursor: null } }));
      }),
    );
    render(<App />);
    expect(screen.getByRole("status")).toHaveAttribute("data-state", "loading");
    release();
    expect(await screen.findByText(/No runs match/)).toHaveAttribute("data-state", "empty");
  });

  it("an incomplete bundle never looks like a completed verdict", () => {
    render(<RunBody run={detail({ completeness: "incomplete", verdict: "INTERRUPTED", report: null })} />);
    expect(screen.getByText(/Incomplete bundle/)).toBeInTheDocument();
    expect(screen.getByText("INTERRUPTED")).toBeInTheDocument();
  });
});

describe("evidence presentation", () => {
  it("separates upstream witness from client outcome and plan from observation", () => {
    serve(() => ({ status: 200, body: EMPTY_EVENTS }));
    render(<RunBody run={detail()} />);
    const table = screen.getByRole("table", { name: "" }) ?? screen.getAllByRole("table")[0];
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("purchase_a/1");
    expect(rows[1]).toHaveTextContent("http_response 201");
    expect(rows[1]).toHaveTextContent("timeout");
    expect(rows[1]).toHaveTextContent("injected_response_suppression");
    // The third planned attempt was never needed: shown from the plan, not invented.
    expect(rows[3]).toHaveTextContent("not dispatched / not needed");
    expect(screen.getByText("acknowledged as applied")).toBeInTheDocument();
  });

  it("ERROR with a violation shows both the harness error and the violation", () => {
    serve(() => ({ status: 200, body: EMPTY_EVENTS }));
    const run = detail({ verdict: "ERROR" });
    run.report!.verdict = "ERROR";
    run.report!.diagnostics = [{ severity: "error", code: "cleanup_failed", message: "left over" }];
    render(<RunBody run={run} />);
    expect(screen.getByText(/Harness errors \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("duplicate_debit")).toBeInTheDocument();
    expect(screen.getByText(/not a trustworthy reproduction/)).toBeInTheDocument();
  });

  it("PASS states its limited scope", () => {
    serve(() => ({ status: 200, body: EMPTY_EVENTS }));
    const run = detail({ verdict: "PASS" });
    run.report!.invariants = [];
    render(<RunBody run={run} />);
    expect(screen.getByRole("note")).toHaveTextContent("not proof of correctness");
  });

  it("64-bit values serialized as strings are displayed exactly", () => {
    render(<Money minor="9007199254740993" />);
    expect(screen.getByText("9007199254740993")).toBeInTheDocument();
    expect(screen.getByText("(₹90071992547409.93)")).toBeInTheDocument();
  });

  it("evidence references resolve only to snapshot record sets", () => {
    expect(parseEvidence("snapshots/final.json#/records/ledger")).toEqual({
      snapshot: "final",
      recordSet: "ledger",
    });
    expect(parseEvidence("../../etc/passwd")).toBeNull();
    expect(parseEvidence("snapshots/final.json#/records/<script>")).toBeNull();
  });

  it("artifact text is rendered as text, never as markup", () => {
    serve(() => ({ status: 200, body: EMPTY_EVENTS }));
    const run = detail();
    run.report!.invariants[0].failures[0].observed = "<img src=x onerror=alert(1)>";
    const { container } = render(<RunBody run={run} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
  });
});

describe("groups", () => {
  it("replay groups show all counts and both denominators", () => {
    const group: GroupRecord = {
      group_id: RUN,
      kind: "replay",
      status: "completed",
      run_ids: [],
      scheduled: 5,
      started: 4,
      completed: 3,
      interrupted: 1,
      unstarted: 1,
      counts: { PASS: 1, FAIL: 2, INCONCLUSIVE: 0, ERROR: 1 },
      matching_failures: 2,
      other_failures: 0,
      target_signature: "e".repeat(64),
      reproduction: {
        per_started: { numerator: 2, denominator: 4, ratio: "2/4" },
        per_conclusive: { numerator: 2, denominator: 0, ratio: "not_available" },
      },
      trials: [],
    };
    render(<Repeats group={group} />);
    expect(screen.getByText("2/4")).toBeInTheDocument();
    expect(screen.getByText("not_available")).toBeInTheDocument();
    expect(screen.getByText(/never count as reproductions/)).toBeInTheDocument();
  });

  it("an unverified reduction is never presented as verified", () => {
    const group: GroupRecord = {
      group_id: RUN,
      kind: "reduction",
      status: "completed",
      run_ids: [],
      outcome: "reduction_unverified",
      termination_reason: "budget_exhausted_before_validation",
      original_objective: [20, 1, 21],
      best: { objective: [1, 1, 2], removed_operations: ["noise_01"], removed_faults: [] },
      validation: { completed: false },
      candidates: [],
    };
    render(<Reduction group={group} />);
    expect(screen.getByText(/NOT verified/)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("not a verified reduced reproduction");
    expect(screen.getByText(/Final validation: not completed \(budget\)/)).toBeInTheDocument();
    expect(screen.queryByText(/minimal reproduction/i)).toBeNull();
    expect(screen.getByText(/not .minimal./)).toBeInTheDocument();
  });
});

describe("keyboard investigation path", () => {
  it("reaches a violation and its ledger rows from the run list by keyboard", async () => {
    const user = userEvent.setup();
    serve((url) => {
      if (url.startsWith("/api/v1/runs?")) {
        return {
          status: 200,
          body: {
            data: [
              {
                run_id: RUN,
                scenario_id: "lost_response_purchase",
                variant: "unsafe",
                created_at: "2026-10-29T10:00:00+00:00",
                verdict: "FAIL",
                origin: "run",
                coverage: { required_applied: 1, required_total: 1 },
              },
            ],
            meta: { next_cursor: null },
          },
        };
      }
      if (url === `/api/v1/runs/${RUN}`) return { status: 200, body: { data: detail(), meta: {} } };
      if (url.includes("/events")) return { status: 200, body: EMPTY_EVENTS };
      if (url.includes("/snapshots/")) {
        const final = url.includes("/snapshots/final");
        const debit = (id: string) => ({
          entry_id: id,
          purchase_id: "purchase-a",
          amount_minor: "200000",
        });
        return {
          status: 200,
          body: {
            data: {
              snapshot_id: final ? "final" : "baseline",
              record_set: "ledger",
              record_sets: ["ledger"],
              rows: final ? [debit("debit-1"), debit("debit-2")] : [],
              total: final ? 2 : 0,
              offset: 0,
              limit: 200,
              complete: true,
              quiescent: true,
              content_hash: "c".repeat(64),
            },
            meta: {},
          },
        };
      }
      return { status: 404, body: { error: { code: "not_found", message: url } } };
    });
    render(<App />);
    await screen.findByText("lost_response_purchase");
    // Tab to the run link and activate it with Enter.
    const runLink = screen.getByRole("link", { name: /run 11111111/ });
    while (document.activeElement !== runLink) await user.tab();
    await user.keyboard("{Enter}");
    const invariant = await screen.findByRole("link", { name: /orders.at_most_one_debit v1/ });
    invariant.focus();
    await user.keyboard("{Enter}");
    await screen.findByText("debit-2");
    expect(screen.getByText("debit-1")).toBeInTheDocument();
    expect(screen.getAllByText(/client saw/)).toHaveLength(2);
    expect(window.location.pathname).toBe(`/runs/${RUN}/invariants/0`);
    navigate("/");
  });
});
