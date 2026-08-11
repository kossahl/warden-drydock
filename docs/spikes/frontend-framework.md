# Frontend toolchain spike: Node/npm, Bun, and Vite

## Status

Decision for the local browser personal pilot: use React and TypeScript with
Vite, a pinned supported Node.js LTS release, and a pinned npm release. Commit
`package-lock.json` and install with `npm ci`.

Bun is not the canonical package manager, runtime, test runner, or bundler for
the MVP. Node.js and npm exist only in developer environments, CI, and the
frontend image-builder stage. The final Python `app` image contains neither
Node.js nor npm nor Bun.

## Question and boundary

The framework choice is already React/TypeScript with a client-side Vite build
served by Python. This spike decides which JavaScript toolchain owns dependency
resolution, scripts, tests, and production bundling.

The pilot is a localhost-only, single-Warden application rather than a public
content site. Loopback is a deployment boundary, not authentication; the pilot
does not claim user identity or access control. Python application services own
all domain and mutation authority.

Tool roles are separate:

- npm resolves and installs the committed dependency graph.
- Vite develops, transpiles, and bundles the browser application.
- TypeScript performs a separate `tsc --noEmit` type check because Vite's
  transpilation is not the type-safety gate.
- Vitest runs unit and component tests.
- Real-browser tests use an actual browser provider for offline, storage,
  service-worker, reload, accessibility, and synchronization behavior.
- Python serves only the completed static build in the final runtime image.

## Compared options

| Concern | A: Node/npm + Vite | B: Bun + Vite | C: Bun-native |
| --- | --- | --- | --- |
| Package authority | npm + committed `package-lock.json` + `npm ci` | Bun package manager and `bun.lock`, or mixed-lock risk | Bun package manager and `bun.lock` |
| Package-script launcher | npm | Bun may replace npm as the launcher | Bun |
| Vite/Vitest runtime | Pinned Node LTS | Pinned Node LTS by default because Bun respects Node shebangs; `bun run --bun ...` is a separate experiment | Bun runtime replaces Node |
| Browser bundler | Vite | Vite | Bun's native bundler replaces Vite |
| Unit/component tests | Vitest on Node | Vitest on pinned Node by default | `bun test` replaces Vitest |
| Type checking | Explicit `tsc --noEmit` | Still requires explicit `tsc --noEmit` | Still requires an explicit TypeScript type-check contract |
| Windows/Linux evidence | Mature common path; verify both with identical frozen commands | Bun supports both, but adds a compatibility variable without an MVP need | Replaces package, runtime, test, and bundler contracts at once |
| Final runtime | Static assets copied into Python image | Same if builder is isolated correctly | Same only after recreating Vite output and test obligations |
| MVP judgment | Selected | Feasible future substitution; no measured need now | Rejected for MVP; materially different toolchain |

### A: Node/npm with Vite

This option gives one lockfile authority and uses each selected tool for its
documented role. npm's `ci` command requires an existing lockfile, rejects a
lockfile that disagrees with `package.json`, removes the prior install tree, and
does not rewrite dependency metadata. Vite produces the browser bundle, while
TypeScript remains an independent type-check gate.

### B: Bun with Vite

Bun can replace npm as package manager and package-script launcher, so this
option is technically viable. By default, Bun respects the Node shebangs on
locally installed CLIs: Vite and Vitest therefore continue under the pinned
Node.js runtime even when launched with `bun run`. It does not remove Vite,
Vitest, TypeScript checks, or real-browser testing. Adopting it would change the
package manager, lockfile, script launcher and shell, lifecycle-script behavior,
platform matrix, and reproducibility evidence. No measured MVP problem
currently justifies that change.

`bun run --bun ...` overrides a CLI's Node shebang and is a separate, unselected
runtime-compatibility experiment. It cannot be introduced implicitly with a
Bun+Vite package-manager migration. Any proposal to use it for Vite, Vitest, or
another CLI must pin the exact command set and pass Windows/Linux compatibility,
test, build-digest, and final-image evidence before a runtime decision changes.

The only safe future Bun+Vite form is a complete, reviewed package-authority
migration: pin an exact Bun version; replace `package-lock.json` with one
canonical `bun.lock`; forbid mixed npm/Bun installs in CI and docs; keep Vite as
the bundler under pinned Node by default; keep `tsc --noEmit`; keep Vitest under
pinned Node and keep the real-browser suites; do not use `bun run --bun` without
its own accepted compatibility experiment; reproduce Windows/Linux and
repeated-build evidence; and prove the final image contains neither Bun nor
Node/npm. Running `bun install` against npm's canonical lockfile as an
unreviewed acceleration shortcut is not allowed.

### C: Bun-native

Bun-native means more than changing the install command. It replaces Vite's
bundling contract and Vitest's test-runner contract with `bun build` and
`bun test`. Replacing Vitest with `bun test` belongs only to this alternative,
not Bun+Vite. A future proposal must therefore specify and verify replacements
for Vite configuration/plugins, asset hashing, base paths, development behavior,
environment handling, service-worker integration, Vitest features/reporting,
coverage, browser-provider integration, module resolution, source maps, and
deterministic bundle manifests. It must also rerun every cross-platform,
real-browser, security, accessibility, and final-image gate. That is a new
architecture decision, not a package-manager optimization.

## Decision

Use option A. Phase 3 pins exact supported Node.js LTS and npm versions in the
repository, CI, and builder image; aliases such as `latest` are forbidden. The
repository commits `package-lock.json`. Local clean installs, CI, and image
builds use `npm ci` with the same project-scoped npm configuration.

Both pins use full `major.minor.patch` values. The selected Node.js patch must
be in Active or Maintenance LTS on the recorded selection date, and the selected
npm patch must declare support for that Node.js version. Updating either pin is
a reviewed dependency/toolchain change with the complete verification sequence.

Vite builds content-hashed static assets and the Python application serves them
with SPA route fallback. Node/npm do not run beside Python. Bun is absent from
the canonical developer, CI, builder, test, and runtime paths.

The frontend communicates only with the versioned same-origin Python API and
SSE stream. It contains no provider credentials, deterministic mutation logic,
filesystem paths, database identifiers, or second authority layer. Browser
persistence remains behind a technology-neutral queue interface until the
device-storage spike is complete.

## Reproducibility and verification contract

The Phase 3 package records exact patch versions in version files, the
`packageManager` field, CI setup, and the builder image. On Windows and Linux,
from a clean checkout, the canonical sequence is:

```text
node --version
npm --version
npm ci
npm run typecheck
npm run test:unit
npm run test:browser
npm run build
```

`typecheck` runs `tsc --noEmit`. `test:unit` uses Vitest. `test:browser` runs
real browser processes, not only a simulated DOM, and covers:

- offline startup and reconnect;
- selected durable-storage adapter semantics;
- service-worker install, update, cache, and failure behavior;
- direct reload on nested SPA routes;
- keyboard, focus, semantics, and automated accessibility checks;
- multi-tab synchronization, observer/takeover, stale controller, operation
  replay/conflict, and **Saved on device** versus **Synced** states.

Two isolated clean builders using the pinned toolchain and lockfile build the
same commit with timestamps and mutable labels excluded. A sorted manifest of
relative output paths and SHA-256 file digests must match. Any nondeterministic
artifact blocks acceptance or is explicitly removed from the canonical build.

The final-image test inspects files, executable paths, environment, process
command, and software inventory. `node --version`, `npm --version`, and
`bun --version` must be unavailable; no Node, npm, Bun, development server, or
JavaScript dependency tree may remain. The Python `app` must still serve hashed
assets and nested-route fallback.

## Consequences

- Dependency authority is singular and reviewable through `package-lock.json`.
- Development and builder prerequisites do not become end-user runtime
  prerequisites.
- Vite transpilation and TypeScript correctness are explicit separate gates.
- Unit/component speed does not substitute for browser behavior evidence.
- A Bun migration remains possible but must prove the whole affected contract.

## Reconsideration gates

Reconsider Bun+Vite only after a measured install, CI, or developer-loop problem
persists under the pinned npm path and a cross-platform migration spike proves
equal or better reproducibility, lifecycle behavior, security, and maintenance.

Reconsider Bun-native only when a product or build requirement cannot be met by
Vite and the replacement obligations above have an implementation-ready plan.
Benchmark speed alone is insufficient for either migration.

Reconsider Next.js only for public SEO/SSR, server-rendered authentication, a
deliberate Next Backend for Frontend, a measured rendering problem, or unified
public marketing/application composition. Remote deployment alone is not a
trigger.

## Primary evidence

These primary official sources were checked on 2026-08-11:

- [Node.js releases and LTS policy](https://nodejs.org/en/about/previous-releases)
- [`npm ci`](https://docs.npmjs.com/cli/commands/npm-ci/)
- [`package-lock.json`](https://docs.npmjs.com/cli/configuring-npm/package-lock-json/)
- [Vite getting started and Node support](https://vite.dev/guide/)
- [Vite TypeScript transpile-only behavior](https://vite.dev/guide/features.html#typescript)
- [Vite production builds](https://vite.dev/guide/build)
- [TypeScript `noEmit`](https://www.typescriptlang.org/tsconfig/noEmit.html)
- [Vitest](https://vitest.dev/guide/)
- [Vitest Browser Mode](https://vitest.dev/guide/browser/)
- [Playwright service-worker testing](https://playwright.dev/docs/service-workers)
- [Playwright accessibility testing](https://playwright.dev/docs/accessibility-testing)
- [Bun overview](https://bun.sh/docs)
- [Bun package installation](https://bun.sh/docs/pm/cli/install)
- [Bun script runtime and `--bun`](https://bun.sh/docs/runtime#--bun)
- [Bun test runner](https://bun.sh/docs/test)
- [Bun bundler](https://bun.sh/docs/bundler)

The sources show that Vite can be invoked by multiple package managers, Bun
respects Node shebangs by default unless `--bun` is selected, and Bun offers an
integrated package manager/runtime/test runner/bundler. They do not make those
roles interchangeable. The MVP selects and verifies one explicit tool for each
role.
