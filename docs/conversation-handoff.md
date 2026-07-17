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
- a generic project template;
- a Mothership adapter;
- a generated standalone example campaign;
- validation and context-building behavior;
- initial ADRs;
- unit tests for campaign generation;
- AI-assisted setup documentation.

The generated example campaign preserves one known campaign fact: the group has completed **Another Bug Hunt**, but the table-specific outcome has not been recorded. The framework must not invent survivors, decisions, recovered material, employers, or consequences.

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

## Immediate engineering priorities

The next likely priorities are:

1. harden the AI-assisted bootstrap experience for Codex and VS Code;
2. define and implement version-lock and non-destructive update behavior;
3. formalize file ownership metadata;
4. improve validation beyond syntax toward campaign semantics;
5. strengthen generated campaign instructions and first-run onboarding;
6. add release packaging so agents can install or invoke Drydock without a source checkout.

## Constraints for future work

- Do not reintroduce submodules as the default user workflow.
- Do not require Obsidian, Notion, or a proprietary cloud service.
- Do not silently overwrite campaign content.
- Do not allow the AI to promote its own ideas into canon.
- Do not make the framework Mothership-specific internally, even though Mothership is the only current adapter.
- Do not overengineer multi-system abstractions before a second real adapter exists.
