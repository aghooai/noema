import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config.ts";

/** Unit and component tests: Node environment, msw for HTTP, react-dom/server for component markup. */
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      include: ["test/**/*.test.ts", "test/**/*.test.tsx"],
      environment: "node",
      testTimeout: 20_000,
      restoreMocks: true,
    },
  }),
);
