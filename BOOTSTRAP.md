# Bootstrap a Warden Drydock Campaign

Warden Drydock is an AI-native tabletop campaign framework. The AI interprets
the Warden's intent; Drydock performs deterministic repository operations. A
generated campaign is a standalone Git repository, and AI-created material is
never canon without explicit human approval.

## Copy this prompt into an empty campaign directory

```text
Set up a new Warden Drydock campaign in the current directory.

Warden Drydock is the deterministic repository tool underneath this AI-assisted
workflow. Its canonical source is:
https://github.com/kossahl/warden-drydock

Use the pinned release v0.1.0. Do not use the default branch, reconstruct the
campaign layout manually, or invent campaign canon.

Follow this procedure exactly:

1. Record the absolute path of the current directory as the campaign target.
   Confirm that it is empty. If it is not empty, stop without changing anything;
   never look for or use an overwrite bypass.
2. Before asking campaign questions, verify that Git and Python 3.11 or newer
   are available. If either prerequisite is missing, stop and tell me exactly
   what must be installed.
3. Create a unique temporary virtual environment in the operating system's
   temporary directory, outside the campaign target. Do not activate it; invoke
   its Python executable by absolute path.
4. Using that virtual environment's Python, run:

   python -m pip install "warden-drydock @ git+https://github.com/kossahl/warden-drydock.git@v0.1.0"

   If network access or installation fails, remove the temporary environment,
   leave the campaign target untouched, and report the exact failure and a
   corrective action.
5. Run `python -m warden_drydock --version` with the temporary environment's
   Python. Continue only if it reports `Warden Drydock 0.1.0`; otherwise clean
   up, leave the campaign untouched, and report the mismatch.
6. Now ask me for the campaign name. Ask only for campaign-specific facts that
   cannot be inferred. Do not invent setting details, prior events, player
   characters, outcomes, or canon.
7. With the temporary environment's Python, run this deterministic command,
   substituting the recorded target and approved campaign name:

   python -m warden_drydock bootstrap CAMPAIGN_TARGET --adapter mothership --name "CAMPAIGN NAME"

8. If bootstrap or its validation fails, do not initialize or commit Git. Keep
   the campaign output available for review, remove the temporary environment,
   and report the exact failed command and error.
9. On success, run the generated campaign's standalone validation command with
   the temporary environment's Python:

   python CAMPAIGN_TARGET/scripts/drydock.py validate

   If it fails, do not initialize Git; report the error as above.
10. After successful validation, initialize Git in the campaign target, stage
    the generated files, and create the first commit with message
    `Initialize Warden Drydock campaign`. If Git identity or commit creation
    fails, preserve the generated campaign and explain how I can correct it.
11. Remove the temporary virtual environment. Explain what was created, list
    the remaining campaign-specific setup questions, and suggest the next
    natural-language request.

At no point should you replace a Drydock command with improvised file generation.
```

## Availability gate

The pinned command above becomes the supported onboarding path only after the
`v0.1.0` tag exists on GitHub and has passed the clean-environment release smoke
test. Before that tag is published, use the local-development procedure in
`docs/ai-assisted-setup.md` instead.
