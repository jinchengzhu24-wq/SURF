import { resolve } from "node:path";
import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/survey/",
  plugins: [preact()],
  resolve: {
    alias: [
      {
        find: "@sokoban/ui-core/styles/tokens.css",
        replacement: resolve(
          import.meta.dirname,
          "../../packages/ui-core/src/styles/tokens.css",
        ),
      },
      {
        find: "@sokoban/ui-core",
        replacement: resolve(
          import.meta.dirname,
          "../../packages/ui-core/src/index.ts",
        ),
      },
    ],
  },
  build: {
    outDir: resolve(import.meta.dirname, "../../dist/survey"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
