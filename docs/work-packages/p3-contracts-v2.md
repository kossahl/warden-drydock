# P3-CONTRACTS v2 amendment

This version supersedes `p3-contracts-v1.md` for the current branch. All v1
scope, authority, acceptance, and non-goals remain binding.

The independent review established that a conforming Draft 2020-12 validator is
required to verify the schemas. Ownership therefore expands narrowly to
`pyproject.toml` solely to add `jsonschema` to the `dev` extra. Runtime
dependencies remain unchanged. The package must also publish and index a
versioned semantic-invariant specification for cross-field rules that JSON
Schema cannot express. No endpoint, runtime API, persistence, or migration
implementation is authorized.

- **Version:** 2
- **Pinned work-package commit:** `b675755`
- **Owned paths:** `docs/contracts/hosted/**`, `tests/hosted/contracts/**`, and
  the `project.optional-dependencies.dev` entry in `pyproject.toml`
- **Recovery:** remove the dev-only dependency and invariant package before any
  dependent implementation; campaign and runtime state are unaffected.
