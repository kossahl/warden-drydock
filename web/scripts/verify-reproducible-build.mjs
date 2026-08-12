import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { relative, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const output = resolve(root, "dist");

function build() {
  const npmCli = process.env.npm_execpath;
  if (!npmCli) throw new Error("npm_execpath is unavailable");
  const result = spawnSync(process.execPath, [npmCli, "run", "build"], {
    cwd: root,
    encoding: "utf8",
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map((entry) => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  }));
  return nested.flat();
}

async function digest() {
  const manifest = [];
  for (const path of (await files(output)).sort()) {
    const fileDigest = createHash("sha256").update(await readFile(path)).digest("hex");
    manifest.push(`${relative(output, path).replaceAll("\\", "/")} ${fileDigest}`);
  }
  return createHash("sha256").update(manifest.join("\n"), "utf8").digest("hex");
}

build();
const first = await digest();
build();
const second = await digest();
console.log(`first=${first}`);
console.log(`second=${second}`);
if (first !== second) {
  console.error("Production build digests differ");
  process.exit(1);
}
