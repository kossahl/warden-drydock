# ADR-001: AI-assisted setup is the primary product interface

## Status

Accepted.

## Decision

Users interact primarily through a repository-capable AI agent. The agent invokes deterministic Warden Drydock commands for initialization, validation, context generation, and later migrations.

## Consequences

- Users do not need to memorize CLI syntax.
- CLI operations remain documented and independently usable.
- Agent instructions are first-class product assets.
- Natural-language interpretation and deterministic filesystem mutation remain separate concerns.
