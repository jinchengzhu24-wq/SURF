import { resolve } from "node:path";
import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/cocreation/",
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
    outDir: resolve(import.meta.dirname, "../../dist/cocreation"),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/cocreation/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/cocreation/, ""),
      },
    },
  },
});
