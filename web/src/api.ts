// Read-only client for the local Faultline API. The bearer token lives only in memory.

export type LoadState =
  | "unauthorized"
  | "not_found"
  | "not_ready"
  | "corrupt"
  | "unsupported"
  | "disconnected"
  | "error";

export class ApiError extends Error {
  state: LoadState;
  status?: number;

  constructor(state: LoadState, message: string, status?: number) {
    super(message);
    this.state = state;
    this.status = status;
  }
}

let token: string | null = null;

/** Take the launch token from the URL fragment once, then remove it from the address bar. */
export function captureToken(location: Location, history: History): string | null {
  const match = /(?:^#|&)token=([A-Za-z0-9_-]{16,128})(?:&|$)/.exec(location.hash);
  if (match) {
    token = match[1];
    history.replaceState(null, "", location.pathname + location.search);
  }
  return token;
}

export function setToken(value: string | null) {
  token = value;
}

export function hasToken(): boolean {
  return token !== null;
}

function stateFor(status: number, code: string | undefined): LoadState {
  if (status === 401) return "unauthorized";
  if (status === 404) return code === "artifact_not_ready" ? "not_ready" : "not_found";
  if (status === 409) return "unsupported";
  if (status === 422) return "corrupt";
  return "error";
}

export interface Envelope<T> {
  data: T;
  meta: Record<string, unknown>;
}

export async function getJson<T>(path: string): Promise<Envelope<T>> {
  if (token === null) throw new ApiError("unauthorized", "No access token in this page");
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("disconnected", "The local Faultline server is not reachable");
  }
  let body: {
    data?: T;
    meta?: Record<string, unknown>;
    error?: { code: string; message: string };
  };
  try {
    body = await response.json();
  } catch {
    throw new ApiError("error", "The server returned a non-JSON response", response.status);
  }
  if (!response.ok || body.error) {
    throw new ApiError(
      stateFor(response.status, body.error?.code),
      body.error?.message ?? `HTTP ${response.status}`,
      response.status,
    );
  }
  return { data: body.data as T, meta: body.meta ?? {} };
}

/** Fetch one inventoried artifact by opaque ID; the token never enters a URL. */
export async function downloadArtifact(runId: string, artifactId: string, name: string) {
  if (token === null) throw new ApiError("unauthorized", "No access token in this page");
  const response = await fetch(`/api/v1/runs/${runId}/artifacts/${artifactId}`, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "omit",
  });
  if (!response.ok) throw new ApiError(stateFor(response.status, undefined), "Download failed");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name.split("/").pop() ?? "artifact";
  link.click();
  URL.revokeObjectURL(url);
}

// --- response shapes (the subset the viewer reads) ---------------------------------

export type Verdict = "PASS" | "FAIL" | "INCONCLUSIVE" | "ERROR" | "RUNNING" | "INTERRUPTED";

export interface RunSummary {
  run_id: string;
  scenario_id: string;
  variant: string;
  created_at: string;
  verdict: Verdict;
  origin: string;
  source_run_id?: string;
  group_id?: string;
  coverage?: { required_applied: number; required_total: number } | null;
}

export interface Failure {
  code: string;
  expected: unknown;
  observed: unknown;
  evidence: string[];
}

export interface Finding {
  invariant_id: string;
  version: number;
  status: "PASS" | "FAIL" | "UNKNOWN";
  logical_id: string;
  failures: Failure[];
  evidence: string[];
  reason?: string;
  signatures: string[];
}

export interface Witness {
  outcome: string;
  status?: number;
  body?: unknown;
}

export interface Attempt {
  attempt_id: string;
  outcome: string;
  status?: number;
  upstream?: Witness;
  [key: string]: unknown;
}

export interface Diagnostic {
  severity?: string;
  code: string;
  message: string;
}

export interface Artifact {
  artifact_id: string;
  name: string;
  bytes: number;
  sha256: string;
}

export interface PlannedOperation {
  id: string;
  logical_id: string;
  retry: { max_attempts: number };
}

export interface PlannedFault {
  id: string;
  kind: string;
  required: boolean;
}

export interface Report {
  verdict: Verdict;
  lifecycle: string;
  invariants: Finding[];
  diagnostics: Diagnostic[];
  coverage: {
    required_applied: number;
    required_total: number;
    applied: string[];
    unapplied: string[];
    workload_complete: boolean;
  };
  attempts: Attempt[];
  cleanup: { complete: boolean; remaining_resources: unknown[] };
}

export interface RunDetail {
  run_id: string;
  completeness: "complete" | "incomplete";
  verdict: Verdict;
  manifest: {
    scenario_id: string;
    variant: string;
    created_at: string;
    origin?: string;
    source_run_id?: string;
    group_id?: string;
    plan_hash: string;
    stage: string;
    comparison?: { differences: Difference[] };
  };
  report: Report | null;
  plan: null | { scenario: { operations: PlannedOperation[]; faults: PlannedFault[] } };
  artifacts: Artifact[];
  snapshots: string[];
  diagnostics: Diagnostic[];
}

export interface HistoryEvent {
  event_id: string;
  sequence: number;
  type: string;
  source: string;
  wall_time: string;
  source_monotonic_ns: string;
  operation_id?: string;
  attempt_id?: string;
  fault_id?: string;
  caused_by_event_ids?: string[];
  payload: Record<string, unknown>;
}

export interface EventPage {
  events: HistoryEvent[];
  next_after_sequence: number;
  has_more: boolean;
}

export interface SnapshotPage {
  snapshot_id: string;
  record_set: string;
  record_sets: string[];
  rows: Record<string, unknown>[];
  total: number;
  offset: number;
  limit: number;
  complete: boolean;
  quiescent: boolean;
  content_hash: string;
}

export interface Fraction {
  numerator: number;
  denominator: number;
  ratio: string;
}

export interface Difference {
  field: string;
  source: unknown;
  current: unknown;
}

export interface Candidate {
  index: number;
  phase: string;
  removed: Record<string, unknown>;
  objective: number[];
  valid: boolean;
  accepted?: boolean;
  reason: string;
  trials: { run_id: string; verdict: string; matches_target: boolean }[];
}

export interface Series {
  scheduled: number;
  matching: number;
  errors: number;
  stable: boolean;
  completed?: boolean;
}

export interface GroupRecord {
  group_id: string;
  kind: "repeated_run" | "replay" | "comparison" | "reduction";
  status: string;
  run_ids: string[];
  source_run_id?: string;
  variant?: string;
  source_variant?: string;
  differences?: Difference[];
  counts?: Record<string, number>;
  scheduled?: number;
  started?: number;
  completed?: number;
  interrupted?: number;
  unstarted?: number;
  matching_failures?: number | null;
  other_failures?: number | null;
  target_signature?: string | null;
  reproduction?: { per_started: Fraction; per_conclusive: Fraction };
  trials?: { run_id: string; verdict: string; lifecycle: string; matches_target: boolean }[];
  outcome?: string | null;
  termination_reason?: string | null;
  target?: { invariant_id: string; code: string; logical_id: string };
  original_objective?: number[];
  best?: null | { objective: number[]; removed_operations: string[]; removed_faults: string[] };
  budget?: Record<string, number>;
  baseline?: null | Series;
  validation?: null | Series | { completed: false };
  candidates?: Candidate[];
}
