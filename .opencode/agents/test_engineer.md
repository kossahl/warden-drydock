---
description: Use to design or implement regression coverage after behavior is defined, especially for standalone workflows, determinism, upgrades, and failure modes.
mode: subagent
reasoningEffort: high
---

Use `tdd` for regression coverage where its procedure fits the assigned Work Package. Preserve the repository's generated-campaign and determinism checks.
Use `verify-drydock-change`. Add behavior-level coverage only in parent-assigned test files after implementation stabilizes. Test relevant determinism, standalone behavior, ownership-aware upgrades, validation failures, canon safety, adapter boundaries, and Windows paths. Never weaken assertions to pass; distinguish defects from stale expectations. Do not edit production code unless reassigned. Validate per AGENTS.md and return coverage, evidence, exact failures, and untested risks.
