import { resolve } from "node:path";
import preact from "@preact/preset-vite";
import { defineConfig } from "vitest/config";

export default defineConfig({
  cacheDir: resolve(import.meta.dirname, "node_modules/.vite"),
  plugins: [preact()],
  resolve: {
    alias: {
      "@sokoban/ui-core": resolve(import.meta.dirname, "packages/ui-core/src/index.ts"),
    },
  },
  test: {
    environment: "happy-dom",
    include: ["packages/*/src/**/*.test.{ts,tsx}", "apps/*/src/**/*.test.{ts,tsx}", "packages/*/tests/**/*.test.{ts,tsx}", "apps/*/tests/**/*.test.{ts,tsx}"],
  },
});
