import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * The frontend had no test runner at all until now -- 908 backend tests and
 * zero here, across three releases of UI. This is the runner that makes
 * CLAUDE.md rule 5 ("tests ship with the feature") enforceable on this half of
 * the codebase.
 *
 * jsdom rather than a browser: Cytoscape runs headless, and the logic worth
 * testing (which nodes are navigable, how a threshold is clamped, whether a
 * dialog traps focus) needs no compositor. Anything that genuinely needs
 * pixels belongs in a Playwright suite, which R2 can add.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: { provider: "v8", reporter: ["text-summary"] },
  },
});
