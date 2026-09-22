/// <reference types="vite/client" />

declare const __DRYDOCK_VERSION__: string;

declare module "node:fs" {
  export function readFileSync(path: URL, encoding: "utf8"): string;
}
