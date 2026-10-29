import { type MouseEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Envelope, getJson, type LoadState } from "../api";
import { navigate } from "../router";

/** In-app link: a real anchor, so keyboard, focus, and "open in new tab" keep working. */
export function Link({ to, children, ...rest }: { to: string; children: ReactNode } & Record<string, unknown>) {
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };
  return (
    <a href={to} onClick={onClick} {...rest}>
      {children}
    </a>
  );
}

const VERDICT_ICON: Record<string, string> = {
  PASS: "✓",
  FAIL: "✗",
  INCONCLUSIVE: "?",
  ERROR: "!",
  RUNNING: "…",
  INTERRUPTED: "■",
  UNKNOWN: "?",
};

/** Status is always conveyed by text and a symbol, never by colour alone. */
export function Badge({ value }: { value: string }) {
  return (
    <span className={`badge badge-${value.toLowerCase()}`}>
      <span aria-hidden="true">{VERDICT_ICON[value] ?? "•"} </span>
      {value}
    </span>
  );
}

const STATE_TEXT: Record<LoadState, { title: string; body: string }> = {
  unauthorized: {
    title: "No access token",
    body:
      "This page has no API token, usually because it was reloaded. Reopen the launch URL " +
      "printed by `faultline serve` in your terminal.",
  },
  not_found: { title: "Not found", body: "No run or group with this ID exists in the served root." },
  not_ready: {
    title: "Not ready yet",
    body: "This artifact has not been written yet. Its absence is not evidence about state.",
  },
  corrupt: {
    title: "Corrupt evidence",
    body: "An artifact failed its integrity check. The viewer will not interpret it.",
  },
  unsupported: {
    title: "Unsupported version",
    body: "This bundle uses an artifact version this viewer does not support.",
  },
  disconnected: {
    title: "Disconnected",
    body: "The local Faultline server is not reachable. Is `faultline serve` still running?",
  },
  error: { title: "Server error", body: "The server could not complete this request." },
};

export function StateMessage({ error }: { error: ApiError }) {
  const text = STATE_TEXT[error.state];
  return (
    <section className={`state state-${error.state}`} role="alert" data-state={error.state}>
      <h2>{text.title}</h2>
      <p>{text.body}</p>
      {error.message && error.message !== text.body ? (
        <p className="detail">Detail: {error.message}</p>
      ) : null}
    </section>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p className="loading" role="status" aria-live="polite" data-state="loading">
      Loading {label}…
    </p>
  );
}

export type Resource<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  | { status: "ready"; value: Envelope<T> };

/** Fetch a resource; optionally poll every `pollMs` (at least 2 s) while `active` holds. */
export function useResource<T>(
  path: string | null,
  options: { pollMs?: number; active?: (value: T) => boolean } = {},
): [Resource<T>, () => void] {
  const [state, setState] = useState<Resource<T>>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const options_ = useRef(options);
  options_.current = options;
  const reload = useCallback(() => setTick((value) => value + 1), []);
  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const value = await getJson<T>(path);
        if (cancelled) return;
        setState({ status: "ready", value });
        const { pollMs, active } = options_.current;
        if (pollMs && active?.(value.data)) {
          timer = setTimeout(load, Math.max(2000, pollMs));
        }
      } catch (error) {
        if (cancelled) return;
        setState({
          status: "error",
          error: error instanceof ApiError ? error : new ApiError("error", String(error)),
        });
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [path, tick]);
  return [state, reload];
}

/** Render a JSON value exactly; large integers arrive as decimal strings and stay strings. */
export function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <code>—</code>;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <code>{String(value)}</code>;
  }
  return <code className="json">{JSON.stringify(value, null, 1)}</code>;
}

/** Paise strings to rupees without floating point; unknown shapes are shown verbatim. */
export function Money({ minor }: { minor: unknown }) {
  const text = String(minor);
  if (!/^-?\d+$/.test(text)) return <Value value={minor} />;
  const negative = text.startsWith("-");
  const digits = negative ? text.slice(1) : text;
  const padded = digits.padStart(3, "0");
  const rupees = `${negative ? "-" : ""}₹${padded.slice(0, -2)}.${padded.slice(-2)}`;
  return (
    <span>
      <code>{text}</code> paise <span className="muted">({rupees})</span>
    </span>
  );
}
