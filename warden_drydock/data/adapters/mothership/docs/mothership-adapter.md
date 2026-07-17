# Mothership Adapter

The initial adapter contributes Mothership-oriented Warden principles and entity templates. It intentionally does not reproduce proprietary game text or rules content.

Its declarative `00-drydock/adapter.json` registers adventures, factions, NPCs,
and session logs. The portable Drydock maintenance command uses that registry
for deterministic entity creation and required-frontmatter validation; no
Mothership behavior is hard-coded into the framework core.
