import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  // Adapter definitions and templates are authoritative repository assets
  // outside the web package. Allow Vite to read them during tests and builds.
  server: {
    fs: {
      allow: [".."],
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./tests/unit/setup.ts",
    globals: true,
    include: ["tests/unit/**/*.test.{ts,tsx}"],
  },
});
