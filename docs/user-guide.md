# Warden Drydock User Guide

## What the user needs to know

Warden Drydock is operated through a repository-capable AI agent. Campaign
content remains normal Markdown in a normal Git repository, but the user does
not need to memorize repository layouts or Python commands.

The human Warden remains the authority. AI-created ideas are provisional until
the Warden explicitly approves them as canon.

## Start a campaign

Open an empty campaign directory and give the agent the complete prompt from
[`BOOTSTRAP.md`](../BOOTSTRAP.md). That prompt is deliberately self-contained:
it explains Drydock, identifies the pinned GitHub release, verifies Python and
Git, installs into a temporary environment outside the campaign, and defines
safe failure behavior.

The short request is:

```text
Create a new Warden Drydock campaign in this directory.
Use the Mothership adapter.
Ask only for campaign-specific facts that cannot be inferred.
Follow the complete bootstrap contract at
https://github.com/kossahl/warden-drydock/blob/v0.1.0/BOOTSTRAP.md. Do not
invent campaign canon.
```

The deterministic operation underneath that request is:

```bash
drydock bootstrap . --adapter mothership --interactive
```

Bootstrap refuses a non-empty directory. It installs agent instructions and a
standalone maintenance script, builds initial AI context, and validates the
campaign. The agent reviews the result before initializing Git.

## Continue a campaign

At the start of a fresh agent session, say:

```text
Read AGENTS.md and 00-drydock/ai-context.md before taking action. Treat Git as
the source of truth, preserve the canon gate, and use Drydock commands for
deterministic repository operations.
```

Natural-language requests can then include:

- "Create an NPC draft named Ripley with ID `npc-ripley`."
- "Capture this faction idea without making it canon."
- "Prepare a situation for the next session; do not prescribe an ending."
- "Record tonight's session as a draft for my review."
- "Promote the reviewed session to canon and rebuild context."
- "Audit unresolved links and duplicate IDs."

For adapter-defined records, the agent invokes `drydock new` or the campaign's
local `python scripts/drydock.py new` command. It then edits the new provisional
record with the details supplied by the Warden.

## Canon and session records

The lifecycle is `idea -> draft -> review -> canon -> revealed`. The agent must
not promote its own work to canon. Generated AI context includes only session
logs marked `canon`, `revealed`, or the legacy `accepted` state. Draft session
notes therefore cannot silently become established history.

After material changes, the agent runs:

```bash
python scripts/drydock.py validate
python scripts/drydock.py context
```

## Update Drydock-managed files

After installing a newer framework version, ask the agent to preview an update:

```bash
drydock upgrade .
```

Nothing is written during preview. If the proposed changes are correct:

```bash
drydock upgrade . --apply
```

Campaign-owned content and generated context are never overwritten. If a
framework, adapter, or shared file was locally changed and its upstream version
also changed, the upgrade aborts without modifying anything. Resolve the
customization deliberately and preview again.

## Backups and portability

Commit campaign work to Git and back up or push that repository normally. A
generated campaign has no submodule and does not require the Warden Drydock
source repository at runtime. Obsidian is optional; plain text editors and
other repository-capable agents remain supported.
