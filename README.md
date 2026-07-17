# Warden Drydock

**An AI-native campaign development framework, born from Mothership.**

Warden Drydock is designed for people who want to work through an AI coding agent rather than learn a bespoke campaign-management application.

The end-user experience is intentionally simple:

1. Open an empty folder in Codex, Claude Code, or another repository-capable agent.
2. Tell the agent to create a Warden Drydock campaign.
3. The agent runs Drydock's deterministic bootstrap command.
4. Continue managing the campaign through natural language.

The AI is the interface. Drydock is the dependable tool underneath it.

For the complete end-user workflow, see [the user guide](docs/user-guide.md).
Framework contributors should start with [the developer guide](docs/developer-guide.md).
Adapter work is covered by [the adapter guide](docs/adapter-development.md), and
artifact verification by [the release guide](docs/release.md).

## AI-assisted setup

The canonical onboarding contract is [BOOTSTRAP.md](BOOTSTRAP.md). Open an empty
campaign directory and copy its complete prompt into the repository-capable AI
agent. It defines what Drydock is, where to obtain the pinned release, how to
verify it, and how to fail without damaging the campaign directory.

In abbreviated form, the user asks:

```text
Create a new Warden Drydock campaign in this directory.
Use the Mothership adapter.
Ask me only for campaign-specific information that cannot be inferred.
Follow https://github.com/kossahl/warden-drydock/blob/v0.2.0/BOOTSTRAP.md,
run the deterministic bootstrap command, initialize Git only after validation,
and explain what was created.
Do not invent campaign canon.
```

After acquiring and verifying the pinned release, the agent runs:

```bash
drydock bootstrap . --adapter mothership --interactive
```

Framework contributors working from this source checkout instead run:

```bash
python -m warden_drydock bootstrap ../my-campaign --adapter mothership --name "My Campaign"
```

`bootstrap` initializes the standalone campaign, builds its initial AI context,
and validates it using the maintenance script installed in the campaign. The AI
then initializes Git and creates the first commit after reviewing the result.

The lower-level `drydock init` command remains available for development and
workflows that intentionally orchestrate those steps separately.

## Updating a campaign

An agent previews framework and adapter updates before changing a campaign:

```bash
drydock upgrade /path/to/campaign
drydock upgrade /path/to/campaign --apply
```

The generated `.drydock-lock.json` records file ownership and baseline hashes.
Campaign-owned and generated files are never overwritten. Locally modified
managed or shared files block the entire upgrade so the agent can review the
conflict before retrying.

## Creating campaign entities

Agents create adapter-defined entities with a deterministic command instead of
manually reconstructing templates:

```bash
drydock new npc npc-ripley --name "Ripley" --path /path/to/campaign
drydock new faction faction-company --name "The Company" --path /path/to/campaign
drydock new adventure adventure-derelict --name "The Derelict" --path /path/to/campaign
drydock new session session-001 --name "First Contact" --path /path/to/campaign
```

Inside a generated campaign, the equivalent standalone form is `python
scripts/drydock.py new TYPE ID --name "NAME"`. Entity types, templates,
destinations, and required metadata come from the selected adapter.

Mothership 1e supplies 20 narrative record types covering characters, world
elements, factions, NPCs, creatures, ships, items, mysteries, adventures,
campaign pressures, session lifecycle, handouts, and random tables. These
records track fictional state, not numeric characteristics or copied rules.
Player handouts require an explicit audience and are validated against
Warden-only headings.

## Architecture

- `warden_drydock/core/`: system-agnostic project generation and validation
- `warden_drydock/standalone.py`: portable campaign maintenance implementation
- `warden_drydock/data/adapters/`: RPG-system adapter assets
- `warden_drydock/data/project_template/`: generic generated campaign files
- `tests/`: deterministic behavior tests
- `docs/adr/`: architecture decisions

## Design constraints

- Campaign repositories are standalone and user-owned.
- No Git submodules are required.
- The framework never silently overwrites campaign content.
- AI agents invoke deterministic commands rather than reconstructing repositories from memory.
- Mothership is the first adapter, not a hard-coded assumption in the core.

The framework imports the same portable maintenance implementation that is
copied into generated campaigns as `scripts/drydock.py`. Context generation is
stable across unchanged runs and includes only approved session logs (`canon`,
`revealed`, or the legacy `accepted` status).

## Continue development in Codex

For a curated handoff from the original architecture conversation, read:

- `docs/project-brief.md`
- `docs/product-decisions.md`
- `docs/conversation-handoff.md`
- `docs/continue-in-codex.md`

The continuation guide contains a ready-to-paste Codex prompt.

## MVP status

Version `0.2.0` is the narrative-adapter MVP. It supports complete Mothership
campaign inception, situation and mystery design, world-state maintenance,
session preparation/debrief, safe player handouts, semantic validation,
canon-safe context generation, and preview-first ownership-aware upgrades.
The package remains distributed from its pinned GitHub tag rather than a
package registry.
