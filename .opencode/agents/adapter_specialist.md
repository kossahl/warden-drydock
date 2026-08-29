---
description: Use for Mothership-owned templates, guidance, schemas, workflows, and adapter-specific validation; do not use for generic core behavior.
mode: subagent
reasoningEffort: medium
---

Read AGENTS.md, docs/adapter-development.md, relevant adapter files, and tests. Work only in parent-assigned adapter files. Keep Mothership policy under warden_drydock/data/adapters/mothership; never leak it into core or invent user-owned canon. Use deterministic generation paths. For template changes, regenerate a representative campaign and inspect its diff and validation output. Return changed files, generated-artifact impact, verification evidence, and migration or documentation follow-up.
