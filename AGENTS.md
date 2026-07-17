# Warden Drydock Framework Agent Instructions

## Mission

Maintain Warden Drydock as a deterministic framework whose primary user interface is an AI coding agent.

## Required context before architectural or product changes

Read:

1. `README.md`
2. `docs/project-brief.md`
3. `docs/product-decisions.md`
4. `docs/conversation-handoff.md`
5. all accepted ADRs in `docs/adr/`

These files are the durable handoff from the original design conversation.

## Product rule

The AI interprets intent. Drydock performs repeatable repository operations.
Never replace a deterministic initializer, validator, migration, or context builder with improvised file generation in a prompt.

## End-user simplicity

A user should not need to understand Python packaging, submodules, templates, or adapters. The normal workflow is:

1. user asks an AI to create or maintain a campaign;
2. AI invokes Drydock commands;
3. AI reviews results and communicates decisions;
4. campaign remains a normal standalone Git repository.

## Framework boundaries

- Core is RPG-system agnostic.
- System-specific instructions and templates live in adapters.
- Campaign content is never stored in this framework repository.
- Generated campaign repositories never require this source repository at runtime.
- Updates must be non-destructive and reviewable.

## Change protocol

After changes:

```bash
python -m unittest discover -s tests
python -m warden_drydock --help
```

When template behavior changes, regenerate the example campaign and inspect the diff.
