import { ApiError, hasToken } from "./api";
import { GroupView } from "./components/GroupView";
import { InvariantView } from "./components/InvariantView";
import { RunList } from "./components/RunList";
import { RunView } from "./components/RunView";
import { Link, StateMessage } from "./components/common";
import { useRoute } from "./router";

export function App() {
  const route = useRoute();
  let body;
  if (!hasToken()) {
    body = <StateMessage error={new ApiError("unauthorized", "")} />;
  } else if (route.name === "runs") {
    body = <RunList />;
  } else if (route.name === "run") {
    body = <RunView key={route.runId} runId={route.runId} />;
  } else if (route.name === "invariant") {
    body = <InvariantView key={`${route.runId}-${route.index}`} runId={route.runId} index={route.index} />;
  } else if (route.name === "group") {
    body = <GroupView key={route.groupId} groupId={route.groupId} />;
  } else {
    body = <StateMessage error={new ApiError("not_found", "")} />;
  }
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="site">
        <h1>
          <Link to="/">Faultline evidence viewer</Link>
        </h1>
        <p className="muted">Read-only. Nothing here starts, stops, or changes experiments.</p>
      </header>
      <main id="main" tabIndex={-1}>
        {body}
      </main>
    </>
  );
}
