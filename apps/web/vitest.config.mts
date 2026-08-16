

import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Two environments, chosen per file with a `@vitest-environment` docblock.
 *
 * Route handlers and the billing libraries run in `node`, the environment they
 * actually deploy to - a jsdom `fetch`/`Request` would test a different runtime
 * than production uses. Page rendering runs in `jsdom`.
 *
 * `server-only` is aliased to an empty module. In a real build it is the guard
 * that turns "a client component imported the secret-key module" into a build
 * failure; under Vitest there is no client/server graph for it to police, and
 * the guard itself is asserted directly in `security.test.ts`.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "."),
      "server-only": path.resolve(import.meta.dirname, "tests/stubs/server-only.ts"),
    },
  },
  test: {
    environment: "node",
    globals: true,
    // Keep CI and lower-memory laptops deterministic. With Vitest's default
    // worker count, the full jsdom + Next route suite occasionally timed out
    // while terminating idle forks even though every test passed in isolation.
    pool: "forks",
    maxWorkers: 2,
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    setupFiles: ["tests/setup.ts"],
    restoreMocks: true,
    clearMocks: true,
  },
});
