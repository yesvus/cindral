import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

/**
 * The panel's tests run without a DOM: the components under test are pure
 * functions of their props, and a renderer in the dependency list would buy
 * nothing a reader of `describe` cannot already check.
 */
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
