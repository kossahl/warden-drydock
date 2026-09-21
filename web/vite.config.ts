import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";

const versionSource = readFileSync(new URL("../warden_drydock/__init__.py", import.meta.url), "utf8");
const version = versionSource.match(/__version__\s*=\s*["']([^"']+)["']/)?.[1];
if (!version) throw new Error("Unable to read Drydock version");

export default defineConfig({
  plugins: [react()],
  define: { __DRYDOCK_VERSION__: JSON.stringify(version) },
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
