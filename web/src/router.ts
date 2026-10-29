import { useEffect, useState } from "react";

// A tiny History API router. The URL fragment is reserved for the launch token.

export type Route =
  | { name: "runs" }
  | { name: "run"; runId: string }
  | { name: "invariant"; runId: string; index: number }
  | { name: "group"; groupId: string }
  | { name: "unknown" };

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

export function parse(path: string): Route {
  if (path === "/" || path === "") return { name: "runs" };
  let match = new RegExp(`^/runs/(${UUID})/invariants/(\\d{1,4})$`).exec(path);
  if (match) return { name: "invariant", runId: match[1], index: Number(match[2]) };
  match = new RegExp(`^/runs/(${UUID})$`).exec(path);
  if (match) return { name: "run", runId: match[1] };
  match = new RegExp(`^/groups/(${UUID})$`).exec(path);
  if (match) return { name: "group", groupId: match[1] };
  return { name: "unknown" };
}

const listeners = new Set<() => void>();

export function navigate(path: string) {
  window.history.pushState(null, "", path);
  listeners.forEach((listener) => listener());
  window.scrollTo?.(0, 0);
}

export function useRoute(): Route {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    listeners.add(update);
    window.addEventListener("popstate", update);
    return () => {
      listeners.delete(update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return parse(path);
}
