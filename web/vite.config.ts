import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Built assets are packaged inside the Python wheel and served by `faultline serve`.
// They contain no run data and load nothing from third-party origins.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../src/faultline/viewer",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
