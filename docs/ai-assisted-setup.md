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
- Run the Warden Drydock bootstrap command rather than recreating its structure manually.
- Let bootstrap build the AI context and validate the generated campaign through its installed deterministic maintenance script.
- Initialize Git and create the first commit unless this directory is already a repository.
- At the end, explain what was created, identify remaining campaign setup questions, and give me the next natural-language command to continue.
```

## Expected agent operations

With the released package installed:

```bash
drydock bootstrap . --adapter mothership --name "CAMPAIGN NAME"
git init
git add .
git commit -m "Initialize Warden Drydock campaign"
```

For framework development from a source checkout, target a separate empty
directory:

```bash
python -m warden_drydock bootstrap ../my-campaign --adapter mothership --name "CAMPAIGN NAME"
```

Bootstrap deliberately stops before Git initialization. The AI reviews the
generated repository and owns the explicit Git commands; Drydock owns the
repeatable campaign filesystem operations.

## Product boundary

The user should not need to understand these commands. They are documented so agents can execute predictable operations and developers can debug them.
