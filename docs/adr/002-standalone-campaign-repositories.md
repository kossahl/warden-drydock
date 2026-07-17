# ADR-002: Campaign repositories are standalone

## Status

Accepted.

## Decision

Drydock generates a normal, self-contained campaign repository. It does not connect campaigns through Git submodules.

## Consequences

- Cloning, backing up, and opening a campaign remains conventional.
- Framework source is not mixed with campaign canon.
- Generated metadata records framework and adapter versions.
- Future updates use migrations and reviewable patches.
