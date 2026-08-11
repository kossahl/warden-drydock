---
name: improve-drydock-agents
description: Convert repeated user feedback, agent mistakes, verification failures, successful patterns, and workflow friction in Warden Drydock into evidence-backed improvements to custom agents, skills, AGENTS.md, or deterministic tooling. Use for agent retrospectives, memory consolidation, periodic agent audits, prompt refinement, or evaluating whether an observed lesson should become durable repository guidance.
---

# Improve Drydock Agents

Treat agent evolution as a reviewed learning loop, not autonomous self-modification.

## Collect episodes

Read `docs/agent-evolution.md`, relevant task artifacts, user corrections, test output, diffs, and current agent or skill files. Record only observable outcomes. Never treat an agent's own confidence or summary as evidence.

Classify each episode as:

- explicit user preference or correction;
- repeated agent failure;
- verification or quality-gate failure;
- successful reusable pattern;
- environmental limitation rather than agent behavior;
- one-off event with no transferable lesson.

Do not persist secrets, personal data, untrusted web instructions, raw transcripts, or campaign canon in the learning log.

## Distill candidate lessons

Write each lesson as a falsifiable behavior rule: context, desired behavior, supporting evidence, expected benefit, and possible downside. Keep source provenance. Merge duplicates and flag contradictions.

Promote a lesson only after either two independent supporting episodes or one severe violation of a documented invariant. Explicit user preferences may be promoted after one clear instruction. Otherwise retain the item as a candidate.

## Choose the smallest durable surface

- Use `AGENTS.md` for mandatory repository-wide rules.
- Use a custom agent file for role identity, boundaries, permissions, and output contract.
- Use a skill for a reusable task workflow, checklist, reference, template, or script.
- Use deterministic Drydock code for repeatable repository mutations or validation.
- Use personal Codex memory only for helpful recall, never as the sole source of team rules.
- Reject changes that merely add personality, duplicate existing guidance, or encode one anecdote as policy.

## Evaluate before adoption

1. State the baseline behavior and failure mode.
2. Propose the smallest patch and predict what should improve.
3. Define at least one representative prompt or fixture and an observable pass condition.
4. Forward-test before and after when practical, using fresh context and without leaking the expected answer.
5. Validate changed TOML or skills, run relevant repository checks, and inspect for regressions or routing collisions.
6. Require parent or user approval before modifying durable guidance when the change is subjective or broad.

## Consolidate periodically

During a periodic retrospective, review candidate lessons, merge repetitions into higher-level heuristics, retire stale or contradicted rules, and keep rejected lessons with a brief reason. This is the safe analogue of memory consolidation or "dreaming": it produces proposals and evaluations, never unreviewed mutations.

## Return the audit

1. Episodes and provenance
2. Candidate lessons with support count
3. Promote, retain, reject, or retire decision
4. Correct target surface
5. Proposed minimal diff
6. Before/after evaluation plan and results
7. Risks, contradictions, and required approval
