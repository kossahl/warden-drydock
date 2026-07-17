# Conversation Handoff for Codex

> This is a curated handoff from the original ChatGPT architecture conversation.
> It is current as of 2026-07-17.
> `AGENTS.md`, accepted ADRs, and `docs/product-decisions.md` take precedence if this document conflicts with them.

## How the project started

The initial goal was to create a long-lived Mothership campaign workspace. Obsidian and Notion were considered as possible interfaces. The design changed when the user clarified that they wanted to interact with the campaign primarily through AI and already thought of the work as a software-engineering problem.

The central insight became:

> Design an AI-native repository that happens to work with editors and viewers, rather than designing an Obsidian vault that happens to be accessible to AI.

## Why Warden Drydock exists

The user wants to manage campaign design, continuity, preparation, session recording, and world-state changes through natural language. They should rarely need to browse or manually maintain raw Markdown files.

At the same time, the project must avoid making chat history or one AI vendor the source of truth. Durable knowledge therefore lives in Git-backed Markdown, while generated AI context gives agents a compact current-state view.

## Decisions reached in the conversation

### Data and authority

- Git is the canonical source of truth.
- Markdown and frontmatter are the durable storage format.
- Human Warden approval gates canon.
- Played facts override preparation.
- Generated context is a cache, never a competing source of truth.

### Interface

- AI is the primary user interface.
- Codex and Claude Code are possible agents, but neither may be a hard dependency.
- The repository must contain enough instructions for a fresh agent to continue correctly.
- Agents should use deterministic scripts and CLI commands rather than improvise repository structures.

### Framework structure

- Framework and campaign belong in separate repositories.
- Git submodules were rejected as the normal end-user workflow because they introduce unnecessary technical friction.
- Warden Drydock instead generates standalone campaign repositories.
- Future framework updates must be non-destructive and reviewable.

### Modularity

- The core should be RPG-system agnostic.
- System-specific behavior belongs in adapters.
- Only the Mothership adapter needs implementation now.
- Avoid premature abstraction for RPG systems that have not yet been used.

### Product naming

The framework is named **Warden Drydock**.

The name retains the Warden/Mothership heritage and suggests a place where campaigns are built, inspected, repaired, and refitted.

### Obsidian and Notion

- Notion is unnecessary for the current architecture.
- Obsidian may be useful as a read-oriented viewer for backlinks, graphs, and Canvas.
- Obsidian is optional and must not own campaign data or required behavior.

## Current implementation state

The repository currently includes:

- a Python package and CLI;
- deterministic bootstrap and adapter-driven entity creation;
- a generic project template;
- a Mothership adapter;
- temporary generated campaigns used by tests and release smoke checks;
- semantic validation and canon-safe context-building behavior;
- ownership locks and preview-first non-destructive upgrades;
- initial ADRs;
- unit and standalone workflow tests;
- user, developer, adapter, setup, and release documentation.

No campaign content is committed to the framework repository. One known fact
from the original design conversation remains relevant when a real campaign is
created: the group completed **Another Bug Hunt**, but its table-specific
outcome was not recorded. An agent must ask for that outcome rather than invent
survivors, decisions, recovered material, employers, or consequences.

## Desired end-user experience

An end user opens an empty folder with Codex or another repository-capable AI and says, in natural language, that they want a Warden Drydock campaign.

The AI should:

1. ask only for campaign-specific facts that cannot be safely inferred;
2. invoke the Warden Drydock initializer;
3. install the selected adapter;
4. validate the generated repository;
5. build the initial AI context;
6. initialize Git when appropriate;
7. explain what was created;
8. suggest the next natural-language campaign operation.

The user should not need to understand the underlying Python commands.

The cohesive deterministic command for steps 2 through 5 is `drydock
bootstrap`. It runs the generated campaign's local context and validation
commands, while Git initialization remains an explicit agent-reviewed step.

## Post-MVP engineering priorities

The alpha MVP is complete locally. Work beyond it should be prioritized from
real campaign use and release feedback. Likely follow-ups are:

1. publish and verify a public package installation path;
2. test a real framework-version upgrade with migrated campaign fixtures;
3. expand context selection as real campaign scale reveals retrieval needs;
4. add adapter-specific semantics only when concrete Mothership workflows need them;
5. evaluate a second adapter before generalizing the adapter contract further.

## Constraints for future work

- Do not reintroduce submodules as the default user workflow.
- Do not require Obsidian, Notion, or a proprietary cloud service.
- Do not silently overwrite campaign content.
- Do not allow the AI to promote its own ideas into canon.
- Do not make the framework Mothership-specific internally, even though Mothership is the only current adapter.
- Do not overengineer multi-system abstractions before a second real adapter exists.
