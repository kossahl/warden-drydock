# Warden Drydock

**An AI-native campaign development framework, born from Mothership.**

Warden Drydock is designed for people who want to work through an AI coding agent rather than learn a bespoke campaign-management application.

The end-user experience is intentionally simple:

1. Open an empty folder in Codex, Claude Code, or another repository-capable agent.
2. Tell the agent to create a Warden Drydock campaign.
3. The agent runs Drydock's deterministic bootstrap command.
4. Continue managing the campaign through natural language.

The AI is the interface. Drydock is the dependable tool underneath it.

## AI-assisted setup

Give your agent this instruction:

```text
Create a new Warden Drydock campaign in this directory.
Use the Mothership adapter.
Ask me only for campaign-specific information that cannot be inferred.
Run the bootstrap command, initialize Git, and explain what was created.
Do not invent campaign canon.
```

The agent should then run the cohesive onboarding command:

```bash
drydock bootstrap . --adapter mothership --interactive
```

For development from this source checkout:

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
