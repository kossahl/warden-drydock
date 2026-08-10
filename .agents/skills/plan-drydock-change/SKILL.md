---
name: plan-drydock-change
description: Turn a Warden Drydock product idea, feature request, architectural proposal, migration, or multi-step maintenance change into an evidence-backed, realistically scoped, delegation-ready plan. Use for discovery, roadmap evaluation, acceptance criteria, work-package decomposition, or implementation planning before code changes begin.
---

# Plan a Drydock Change

Convert an idea into a decision and an executable plan without inventing evidence or silently expanding scope.

## Establish the problem

1. Read `README.md`, `docs/project-brief.md`, `docs/product-decisions.md`, `docs/conversation-handoff.md`, all accepted ADRs, and the relevant implementation paths.
2. State the user problem, affected user, current friction, and cost of leaving it unchanged.
3. Separate repository evidence, user-provided evidence, assumptions, and unknowns.
4. Ask for a product decision when an unknown would materially change scope or behavior. Otherwise proceed with an explicit assumption.

## Evaluate the opportunity

- Define the desired outcome before choosing a solution.
- State why the change matters now and what evidence would justify deferring it.
- Compare at least two viable approaches for material decisions, including a smaller option or no-change option.
- Evaluate each option against deterministic operation, standalone campaigns, ownership safety, canon approval, adapter boundaries, backward compatibility, and maintenance cost.
- Prefer the smallest change that can validate the hypothesis. Reject speculative abstractions without a current use case.

## Define success

Write observable acceptance criteria. Include unchanged behavior that must remain protected. Add success signals appropriate to the change, such as fewer manual steps, stable repeated output, zero validation warnings, preserved campaign files, or a verified migration path. Do not invent numeric baselines or targets; label them as measurements to establish.

State explicit non-goals and a revisit condition for deferred scope.

## Build the delivery plan

Split work into bounded packages with:

- objective and concrete deliverable;
- file or subsystem ownership;
- dependencies and ordering;
- recommended agent role;
- acceptance criteria;
- verification command or evidence;
- rollback or recovery consideration when state changes.

Parallelize read-heavy discovery and independent file ownership. Sequence writers that could touch the same files. The parent agent owns delegation, user decisions, and synthesis.

## Return this brief

1. Problem and desired outcome
2. Evidence, assumptions, and unknowns
3. Options and tradeoffs
4. Recommendation and rationale
5. Acceptance criteria and protected invariants
6. Non-goals and revisit conditions
7. Ordered work packages with agent and file ownership
8. Risks, migration, rollback, and validation
9. Decision gate: build, explore further, defer, or reject

