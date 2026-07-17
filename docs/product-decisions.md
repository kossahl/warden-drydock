# Authoritative Product Decisions

This document summarizes the active product decisions. Architecture Decision Records remain authoritative for their specific subjects.

## Naming

- Product name: **Warden Drydock**
- Repository name: `warden-drydock`
- Python distribution: `warden-drydock`
- Python module: `warden_drydock`
- Preferred CLI command: `drydock`

## Product metaphor

A drydock is where vessels are constructed, inspected, repaired, and refitted. Warden Drydock applies that model to campaigns: campaigns are created and maintained there, but remain independent artifacts.

The name deliberately preserves the project's Mothership origin while allowing adapters for other RPG systems.

## Framework and campaign separation

The framework and campaign are separate repositories.

The framework repository contains:

- CLI implementation;
- system-agnostic core;
- adapters;
- project templates;
- migrations and validators;
- documentation and tests.

A generated campaign repository contains:

- campaign content;
- installed agent instructions;
- local validation and context commands;
- adapter-specific templates and rules;
- framework and adapter version metadata.

Generated campaigns do not use Git submodules and do not require the framework source repository at runtime.

## Integration model

Warden Drydock is a project generator and maintenance tool, not a library physically embedded in a campaign.

The intended control flow is:

```text
Natural-language instruction
        ↓
Repository-capable AI agent
        ↓
Warden Drydock deterministic command
        ↓
Reviewable repository changes
```

The AI must invoke the initializer, validator, context builder, and future migration commands rather than reconstructing their behavior manually.

## File ownership

Future update behavior should distinguish:

- `framework`: managed by Drydock when unmodified;
- `campaign`: always user-owned and never overwritten;
- `shared`: framework defaults with expected local customization;
- `generated`: rebuilt from source material and not manually edited.

Updates must be non-destructive and reviewable, preferably through a branch or patch when conflicts exist.

The implemented ownership lock records the installed baseline for each
generated path. `drydock upgrade` is preview-only unless `--apply` is supplied,
never updates campaign-owned or generated files, and aborts without changes if
a managed file has a local modification that conflicts with the new baseline.

## Adapter model

The core understands generic concepts such as:

- entities;
- relationships;
- canon and knowledge states;
- sessions and timelines;
- context generation;
- validation;
- migrations.

The Mothership adapter supplies:

- Warden-facing design principles;
- situation-first adventure guidance;
- Stress, Panic, injury, and survival-horror concepts;
- Mothership entity templates;
- Mothership-specific validation and workflows.

Only Mothership needs to work now. The adapter boundary should be real, but abstractions for hypothetical systems should not be overbuilt.

## Canon model

AI-generated material is provisional unless the Warden explicitly approves it.

The lifecycle is:

```text
idea → draft → review → canon → revealed
```

Played events override unplayed preparation. Retcons must be explicit, recorded, and reviewable.

Important information may have separate layers:

- Warden truth;
- player knowledge;
- player belief;
- public or institutional claim.

## Obsidian

Obsidian is an optional viewer. The repository must remain usable in plain text editors, VS Code, Codex, Claude Code, and future tools. No essential information may live only in Obsidian configuration.
