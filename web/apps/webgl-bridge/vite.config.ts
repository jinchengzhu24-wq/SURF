import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  resolve: {
    alias: {
      "@sokoban/ui-core": resolve(
        import.meta.dirname,
        "../../packages/ui-core/src/index.ts",
      ),
    },
  },
  build: {
    outDir: resolve(
      import.meta.dirname,
      "../../../Assets/WebGLTemplates/SokobanPixel/TemplateData",
    ),
    emptyOutDir: false,
    sourcemap: false,
    lib: {
      entry: resolve(import.meta.dirname, "src/main.ts"),
      name: "SokobanWebGLBridge",
      formats: ["iife"],
      fileName: () => "sokoban-bridge.js",
    },
  },
});
