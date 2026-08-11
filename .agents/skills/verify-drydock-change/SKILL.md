---
name: verify-drydock-change
description: Verify a Warden Drydock implementation, bug fix, adapter change, template update, upgrade, or release candidate against acceptance criteria and repository invariants. Use for evidence-based QA, completion checks, regression review, generated-campaign inspection, or readiness decisions after changes are implemented.
---

# Verify a Drydock Change

Judge the change from direct evidence. Do not trust an implementation summary as proof, and do not default to either approval or rejection.

## Establish the verification contract

1. Read `AGENTS.md`, the applicable product decisions and ADRs, the requested acceptance criteria, and the actual diff.
2. Map every claimed behavior to a concrete check. Mark claims without a check as unverified.
3. Identify the high-risk invariants relevant to the change:
   - deterministic output across unchanged runs;
   - standalone generated campaigns;
   - non-destructive ownership-aware updates;
   - canon-safe context selection;
   - core/adapter separation;
   - synchronization of portable standalone behavior;
   - clear failure without partial mutation.

## Gather evidence

- Inspect affected call paths rather than reviewing only the edited lines.
- Run the narrowest reproducer or focused test first.
- Run `python -m unittest discover -s tests` and `python -m warden_drydock --help`.
- When template or generated behavior changes, generate a representative campaign, inspect its diff, validate it, and repeat generation or context building to check stability.
- For upgrades or failures, inspect both the success path and the blocked/rollback path.
- Record exact commands, exit status, and relevant output. Never claim a check ran when it did not.

## Classify results

Use only these verdicts:

- `PASS`: every acceptance criterion and relevant invariant has direct evidence.
- `NEEDS WORK`: one or more criteria fail or a material regression is demonstrated.
- `BLOCKED`: required evidence cannot be obtained; name the missing access, fixture, decision, or environment.

Do not use scores or confidence percentages. A clean test suite does not prove behavior that the tests do not cover.

## Return the evidence report

1. Verdict
2. Acceptance-criterion-to-evidence mapping
3. Commands and outcomes
4. Findings ordered by impact, with file and line references
5. Generated and standalone artifact observations
6. Unverified claims and residual risks
7. Smallest next action needed for `PASS`
