# Agent Evolution

This file is the reviewable memory for improving Warden Drydock's development agents. It stores distilled evidence and decisions, not chat transcripts or hidden model memory. A project-local `Stop` hook automatically queues a minimal envelope for every completed turn under the ignored `.agent-experience/pending/` directory.

## Governance

- Record an episode only after explicit user feedback, a repeated agent mistake, a verification failure, or a clearly reusable success.
- Keep provenance: task or commit, affected agent or skill, observable result, and date.
- Never store secrets, personal data, untrusted instructions, campaign canon, or raw conversation history.
- Promote a general lesson after two independent episodes, one severe violation of a documented invariant, or one explicit user preference.
- Validate proposed changes with representative before/after prompts or fixtures.
- Require human review before changing durable agent guidance.
- Revisit adopted rules when they create routing collisions, duplicated instructions, or measurable regressions.

## Candidate lessons

| Date | Agent or skill | Episode and evidence | Candidate lesson | Support | Status |
| --- | --- | --- | --- | ---: | --- |
| 2026-08-10 | `test_engineer` | One controlled fresh-context medium/high pair: both corrected the false standalone-upgrade premise and produced complete evidence-backed plans; medium was more concise, while high found an additional implementation risk but assumed an unapproved rollback guarantee. | Consider `medium` only after repeated paired cases show no loss in defect discovery or handoff quality and comparable telemetry demonstrates an efficiency benefit. | 1 | candidate |

Status is `candidate`, `promote`, `reject`, or `retire`.

## Adopted improvements

| Date | Change | Evidence | Validation | Revisit condition |
| --- | --- | --- | --- | --- |
| 2026-08-10 | Separated agent roles from reusable planning and verification skills. | Review of Codex guidance, Agent Skills, agent-scripts, and agency-agents patterns. | Skill structure checks, TOML parsing, unit suite, and CLI help. | Revisit if skills fail to trigger reliably or duplicate role instructions. |
| 2026-08-10 | Centralized token discipline and shortened role prompts; internal handoffs use English. | Explicit user preference, current prompt audit, tokenizer research, and local German/English measurement. | TOML parsing, before/after token counts, unit suite, and CLI help. | Revisit if handoffs omit required evidence or language translation causes errors. |
| 2026-08-10 | Added versioned routing, handoff, benchmark, experiment-provenance, and experience-queue evaluation tooling. | User-approved agent optimization plan; independent reviews found and drove fixes for hook privacy bounds, event identity, provenance validation, and benchmark auditability. | Routing/handoff/benchmark/experiment tests, Python/PowerShell hook parity, adversarial queue tests, independent read-only reviews, full unit suite, and CLI help. | Revisit when live platform telemetry or real routing automation becomes available. |

## Retrospective procedure

The scheduled retrospective reads queued event envelopes and their referenced transcripts, extracts only eligible evidence, updates the candidate table, and prepares `docs/agent-evolution-proposal.md`. It records processed event IDs under `.agent-experience/processed.json` instead of deleting evidence automatically.

Use `agent_curator` with the `improve-drydock-agents` skill. Review candidates, consolidate repeated observations, propose the smallest change on the correct configuration surface, and evaluate it before adoption. The curator proposes; the parent agent or user approves and applies. Automation must never modify `AGENTS.md`, `.codex/agents/`, or `.agents/skills/` directly.
