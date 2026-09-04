---
description: Select and execute the next eligible Hosted MVP Work Package from GitHub
agent: drydock-orchestrator
---

Read `AGENTS.md` and private GitHub Project 1. Derive the next eligible Hosted
MVP Work Package from its current fields, dependencies, references, and remote
`master` baseline. Do not use conversation history as authority.

Return `DECISION REQUIRED` or `BLOCKED` and stop if the package is ambiguous or
stale. Otherwise execute it through the configured role agents. Roles may use
Matt's skills as procedures: planning uses `to-spec` or `to-tickets`,
architecture uses `codebase-design`, implementation uses `tdd` and `implement`,
design uses `prototype`, and review uses `verify-drydock-change` plus
`code-review`.

Keep Work Package selection, ownership, branch and pull-request control,
GitHub communication, independent review, required checks, and the structured
handoff under the Drydock workflow. Keep GitHub communication sanitized and
parent-owned.
