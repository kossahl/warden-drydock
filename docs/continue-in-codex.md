# Continue the Warden Drydock Project in Codex

## Recommended method

1. Extract or clone the `warden-drydock` framework repository.
2. Open that repository as the workspace in VS Code.
3. Open the Codex extension.
4. Start a new Codex conversation in the repository.
5. Paste the continuation prompt below.

## Continuation prompt

```text
We are continuing an existing architecture and implementation project named Warden Drydock.

Before making any changes, read:
- AGENTS.md
- README.md
- docs/project-brief.md
- docs/product-decisions.md
- docs/conversation-handoff.md
- docs/ai-assisted-setup.md
- every accepted ADR in docs/adr/

Treat those repository files as the authoritative handoff from the earlier ChatGPT conversation. Do not rely on assumptions from the product name alone.

Then inspect the current implementation and tests.

Report:
1. your understanding of the product and intended end-user workflow;
2. the current repository architecture;
3. any inconsistencies between implementation and documented decisions;
4. the three highest-value next engineering tasks.

Do not modify files yet.
```

## After Codex reports back

Use this follow-up prompt to begin implementation:

```text
Proceed with the highest-value next task you identified.

Requirements:
- preserve the AI-assisted onboarding path as the primary UX;
- use deterministic commands underneath the AI;
- keep generated campaigns standalone;
- do not introduce Git submodules;
- keep the core system-agnostic and Mothership in an adapter;
- update tests and documentation;
- run the complete test suite before finishing;
- summarize changed files, design consequences, and remaining risks.
```

## Continuing the campaign rather than the framework

Open the generated campaign repository instead and tell Codex:

```text
Read AGENTS.md, README.md, START_HERE.md, 00-system/ai-context.md, and all system instructions before making changes.

This campaign was created by Warden Drydock. The human Warden has final authority, and no AI-generated material becomes canon without explicit approval.

First explain the current campaign state and list the missing factual information required to record the table-specific outcome of Another Bug Hunt. Do not invent any campaign facts and do not modify files yet.
```

## Full raw chat

A complete raw transcript is not required and is usually less useful than the curated repository handoff. If a transcript is added later, store it under `docs/history/` and mark it as historical and non-authoritative. Accepted ADRs and product-decision documents must take precedence.
