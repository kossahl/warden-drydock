# Warden Drydock web foundation

This directory contains the React/TypeScript/Vite single-page application. The
canonical build toolchain is Node.js `24.11.1` with npm `11.6.2`; both are build
tools only and are absent from the production Python runtime.

```bash
npm ci
npm run typecheck
npm run test:unit
npm run test:browser
npm run build
```

`package-lock.json` is the only dependency lock. `npm ci` is the frozen install
contract. The build output is `dist/`; it is not committed.

## Integration boundary

`static-integration.json` is the serving contract for the future Python app:
unknown HTML `GET` routes fall back to `dist/index.html`, hashed assets are
served normally, and `/api/` is excluded from fallback. The browser smoke suite
exercises that contract with a test-only Python static server.

The client boundary accepts injected, versioned contract transport. Read models
and `operation_request` intents follow the accepted v1 envelopes; the browser
does not choose backend routes or expose provider invocation, filesystem,
shell, database, publication, apply, or authority-promotion capabilities.
