---
description: Select and execute the next eligible Hosted MVP Work Package from GitHub
agent: drydock-orchestrator
---

Read `AGENTS.md` and private GitHub Project 1. Derive the next eligible Hosted
MVP Work Package from its current fields, dependencies, references, and remote
`master` baseline. Do not use conversation history as authority.

Return `DECISION REQUIRED` or `BLOCKED` and stop if the package is ambiguous or
stale. Otherwise execute it through the configured role agents, independent
review and correction loops, focused branch and pull request, required checks,
and structured handoff. Keep GitHub communication sanitized and parent-owned.
