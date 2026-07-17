# AI-Assisted Campaign Setup

This is the primary Warden Drydock onboarding path.

## What the user does

1. Create or open an empty folder with a repository-capable AI agent.
2. Give the agent the bootstrap prompt below.
3. Answer only campaign-specific questions.
4. Review the generated repository and initial commit.

## Bootstrap prompt

```text
Set up a new Warden Drydock campaign in the current directory.

Requirements:
- Use the Mothership adapter.
- Treat the AI as the user interface and Warden Drydock as the deterministic tool underneath it.
- Ask only for campaign-specific facts that cannot be inferred, starting with the campaign name.
- Do not invent setting details, prior events, player characters, or canon.
- Run the Warden Drydock initializer rather than recreating its structure manually.
- Build the AI context and validate the generated campaign.
- Initialize Git and create the first commit unless this directory is already a repository.
- At the end, explain what was created, identify remaining campaign setup questions, and give me the next natural-language command to continue.
```

## Expected agent operations

From a Warden Drydock source checkout:

```bash
python -m warden_drydock init . --adapter mothership --name "CAMPAIGN NAME"
python scripts/drydock.py context
python scripts/drydock.py validate
git init
git add .
git commit -m "Initialize Warden Drydock campaign"
```

Once released as a package, the agent should install or execute the published package rather than requiring a source checkout.

## Product boundary

The user should not need to understand these commands. They are documented so agents can execute predictable operations and developers can debug them.
