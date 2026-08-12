import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./tests/unit/setup.ts",
    globals: true,
    include: ["tests/unit/**/*.test.{ts,tsx}"],
  },
});
