# Agent Evolution

This file is the reviewable memory for improving Warden Drydock's development agents. It stores distilled evidence and decisions, not chat transcripts or hidden model memory. Agent Ascendry's project-local `Stop` hook automatically queues bounded metadata for completed turns under the ignored `.agent-ascendry/pending/` directory. Transcript content is neither captured nor required by this workflow.

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
| 2026-08-10 | Added versioned routing, handoff, benchmark, experiment-provenance, and portable Agent Ascendry capture/audit tooling. | User-approved agent optimization and extraction plans; independent reviews found and drove fixes for hook privacy bounds, event identity, provenance validation, benchmark auditability, and zero-touch integration. | Routing/handoff/benchmark/experiment tests, wheel-only Agent Ascendry parity tests, adversarial event tests, independent reviews, full unit suite, and CLI help. | Revisit when live platform telemetry or real routing automation becomes available. |

## Retrospective procedure

Run `agent-ascendry audit .` before a retrospective. Use `agent_curator` with the `improve-drydock-agents` skill to review only eligible evidence, consolidate repeated observations, update the candidate table, and prepare the smallest change on the correct configuration surface. Do not open transcript contents or infer missing evidence from a transcript reference.

The curator prepares a schema-v1 proposal input and records it with `agent-ascendry propose . --input FILE`. A human must review the exact proposal and explicitly bind approval to its SHA-256 hash with `agent-ascendry approve`; only then may the parent run `agent-ascendry apply`. Validation failure rolls back local changes, and the result remains reviewable in Git. Neither the curator nor unattended automation may approve a proposal or modify `AGENTS.md`, `.codex/agents/`, or `.agents/skills/` directly.
