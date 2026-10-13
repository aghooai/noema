import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const sdk = fileURLToPath(new URL("../sdk/typescript/src/index.ts", import.meta.url));

/**
 * Production build of the SPA (served by server/main.ts, which adds the spec-09 §8 headers). Nothing is inlined:
 * no data: assets (CSP img-src 'self' blob:), no inline scripts or styles, no source maps in dist.
 */
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@aghoo/sdk": sdk } },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    sourcemap: false,
    modulePreload: { polyfill: false },
    target: "es2022",
  },
});
