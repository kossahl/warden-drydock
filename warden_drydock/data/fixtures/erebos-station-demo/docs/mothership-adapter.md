# Mothership Adapter

The 0.2 adapter contributes Mothership 1e-oriented Warden principles, 20
narrative entity types, secrecy rules, and lifecycle guidance. It intentionally
does not reproduce proprietary game text, numeric stat blocks, or rules procedures.

Its declarative `00-drydock/adapter.json` registers the complete narrative
campaign lifecycle. The portable Drydock maintenance command uses that registry
for deterministic entity creation and required-frontmatter validation; no
Mothership behavior is hard-coded into the framework core.

## Canonical record locations

New factions, NPCs, adventures, and session logs are created in `04-factions`,
`05-npcs`, `10-adventures/available`, and `12-sessions/logs`, respectively.

Campaign-owned files previously created in `06-factions`,
`05-characters/npcs`, or directly inside `10-adventures` remain valid. Drydock
warns about those legacy locations but never moves or rewrites them during an
upgrade. A Warden may manually move a file to its canonical directory after
reviewing and updating any links; this is optional.
