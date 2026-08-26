# Agent Evolution

This file is the reviewable memory for improving Warden Drydock's development
agents. It stores distilled evidence and decisions, not chat transcripts or
hidden model memory. Evidence is supplied explicitly for a requested
retrospective; the repository performs no automatic turn capture.

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

## Retrospective procedure

Use `agent_curator` with the `improve-drydock-agents` skill only when a user
explicitly requests a retrospective. Review supplied eligible evidence,
consolidate repeated observations, update the candidate table, and propose the
smallest change on the correct configuration surface. Do not open transcript
contents or infer missing evidence from a transcript reference.

The curator remains read-only. A human reviews any proposed guidance change;
the parent implements it through the normal branch, pull-request, validation,
and approval workflow. Neither the curator nor unattended automation may
approve or directly modify `AGENTS.md`, `.codex/agents/`, or `.agents/skills/`.
