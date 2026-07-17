# ADR-003: RPG systems are adapters

## Status

Accepted.

## Decision

Generic concepts belong to core. Mothership-specific philosophy, templates, metadata, and validation belong to the Mothership adapter.

## Consequences

- Only Mothership is implemented initially.
- Core abstractions must be justified by current Mothership requirements.
- Future systems can extend the framework without forking it.
