# Mothership Adapter

The initial adapter contributes Mothership-oriented Warden principles and entity templates. It intentionally does not reproduce proprietary game text or rules content.

Its declarative `00-drydock/adapter.json` registers adventures, factions, NPCs,
and session logs. The portable Drydock maintenance command uses that registry
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
