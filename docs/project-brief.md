# Warden Drydock Project Brief

## Product

**Warden Drydock** is an AI-native tabletop campaign development framework, born from Mothership.

It is not primarily a note-taking application, wiki, or GUI. Its intended interface is a repository-capable AI agent such as Codex or Claude Code. The agent interprets the user's natural-language intent; Warden Drydock performs deterministic repository operations underneath it.

## Product goal

Enable an end user with little technical knowledge to:

1. open an empty folder in an AI-enabled development environment;
2. ask the AI to create a campaign;
3. receive a complete, standalone Git repository;
4. continue designing, preparing, running, and recording the campaign through natural language.

## Foundational principles

- Git is the source of truth.
- Markdown with structured frontmatter is the canonical storage format.
- AI is the primary interface.
- Deterministic commands implement repository operations.
- Human review gates canon.
- Campaign repositories are standalone and user-owned.
- No editor or AI vendor owns the data.
- Core functionality remains RPG-system agnostic.
- System-specific behavior is supplied by adapters.
- Mothership is the first and currently only adapter.

## Primary onboarding path

The preferred onboarding path is **AI-assisted setup**.

The user should not need to know package installation, repository layout, adapters, templates, or Git commands. They describe the campaign they want; the AI asks only for facts that cannot safely be inferred and invokes the Warden Drydock CLI.

## Current scope

The current implementation provides:

- a Python CLI;
- a cohesive bootstrap command for AI-assisted onboarding;
- standalone campaign generation;
- ownership-aware, preview-first framework upgrades;
- a system-agnostic core;
- a Mothership adapter;
- generated campaign validation;
- generated AI-context construction;
- agent instruction files;
- architecture decision records;
- deterministic generator tests.

The primary setup operation is `drydock bootstrap`. It initializes a campaign,
builds the generated AI context, and validates the result using the standalone
maintenance script installed into that campaign. Git initialization remains an
explicit, agent-reviewed follow-up operation.

The installed script and framework commands share one source implementation.
Generated AI context excludes provisional session logs and contains only
sessions whose status indicates human approval.

Generated repositories contain `.drydock-lock.json`, which records ownership
and installed hashes. Framework upgrades are all-or-nothing when conflicts are
detected and require an explicit `--apply` flag.

## Explicit non-goals for the current milestone

- A bespoke web application or desktop GUI.
- Notion integration.
- Obsidian as a required dependency.
- Git submodules between framework and campaigns.
- Multiple fully implemented RPG adapters.
- Automatic canon creation without Warden approval.
- Cloud services operated by the project.

## Next milestone: local browser personal pilot

The next milestone adds a browser-based Warden workspace without replacing the
released standalone campaign workflow. A single Warden runs the application
through Docker Compose on `localhost`; ordinary campaign work after startup
does not require Python, Git, Node, package managers, or terminal commands.

The pilot covers deterministic campaign creation, a complete Campaign Atlas,
AI-assisted preparation and canon-grounded live play, offline-safe table-fact
capture, explicit proposal review, and immutable campaign revisions. The Atlas
provides a filterable record library, focused relationship graph with a text
fallback, and an approved-history timeline.

AI is required for the useful product experience. Before entering a campaign
workspace, the Warden must configure the supported provider and explicitly opt
in to sending the minimal campaign excerpts selected by deterministic
retrieval. Changing provider or materially changing data handling requires new
consent. Removing the configuration returns the application to its setup gate.

Campaign ZIP import and revision export are not part of this pilot. Existing
campaign data may be transferred manually during development; a supported
portable import/export workflow is deferred until pilot evidence justifies its
authority, migration, and recovery requirements.

The milestone protects the existing invariants: Markdown and frontmatter remain
durable campaign data, deterministic Drydock operations perform mutations,
the Warden gates canon, campaigns remain portable standalone artifacts, and
the CLI workflow continues to work independently of the browser application.
