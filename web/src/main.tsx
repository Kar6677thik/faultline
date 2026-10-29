import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { captureToken } from "./api";
import { App } from "./App";
import "./styles.css";

// Read the launch token from the fragment, keep it in memory only, and clear the address bar.
captureToken(window.location, window.history);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
