# AI-Assisted Campaign Setup

This is the primary Warden Drydock onboarding path.

The authoritative, copyable agent contract is [`BOOTSTRAP.md`](../BOOTSTRAP.md).
It assumes the agent starts with no knowledge of Drydock and supplies the source
URL, pinned version, acquisition, verification, failure, cleanup, bootstrap,
validation, and Git procedures.

## What the user does

1. Create or open an empty folder with a repository-capable AI agent.
2. Give the agent the complete prompt from `BOOTSTRAP.md`.
3. Answer only campaign-specific questions.
4. Review the generated repository and initial commit.

## Bootstrap prompt summary

This summary is not a replacement for the canonical contract:

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
- Follow the acquisition, version verification, failure, and cleanup contract in BOOTSTRAP.md.
```

## Pinned GitHub onboarding

The canonical prompt creates a temporary virtual environment outside the empty
campaign directory and installs:

```bash
python -m pip install "warden-drydock @ git+https://github.com/kossahl/warden-drydock.git@v0.1.0"
python -m warden_drydock --version
```

The agent must observe `Warden Drydock 0.1.0` before asking for the campaign
name or writing campaign files. This path is supported only after the tag's
release gate has passed.

## Expected campaign operations

With the pinned release verified:

```bash
drydock bootstrap . --adapter mothership --name "CAMPAIGN NAME"
git init
git add .
git commit -m "Initialize Warden Drydock campaign"
```

## Local framework development

Before `v0.1.0` is tagged, or while developing the framework, target a
separate empty directory:

```bash
python -m warden_drydock bootstrap ../my-campaign --adapter mothership --name "CAMPAIGN NAME"
```

Bootstrap deliberately stops before Git initialization. The AI reviews the
generated repository and owns the explicit Git commands; Drydock owns the
repeatable campaign filesystem operations.

If acquisition, verification, bootstrap, or validation fails, the agent reports
the exact failure and does not initialize Git. Prerequisite failures leave the
campaign directory untouched; bootstrap failures remain available for review.

## Product boundary

The user should not need to understand these commands. They are documented so agents can execute predictable operations and developers can debug them.
