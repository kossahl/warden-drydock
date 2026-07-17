# Release Guide

## Prepare

1. Ensure the working tree is clean and all accepted decisions are reflected in
   documentation.
2. Set `warden_drydock.__version__`; package metadata and generated manifests
   read that value dynamically.
3. Update user-facing release notes when a changelog is introduced.

## Verify

```bash
python -m unittest discover -s tests -v
python -m warden_drydock --help
python -m build
```

Install the wheel into an isolated environment, change to a directory outside
the source checkout, and run:

```bash
drydock bootstrap campaign --adapter mothership --name "Release Smoke Test"
cd campaign
python scripts/drydock.py new npc npc-smoke --name "Smoke Test"
python scripts/drydock.py validate
python scripts/drydock.py context
```

Inspect the generated repository, `.drydock.json`, `.drydock-lock.json`, and AI
context. Confirm that packaged templates and adapter declarations are present.

## Publish

Publishing is intentionally not automated in the MVP. A maintainer must review
the artifacts and explicitly upload them to the chosen registry. Do not add a
public installation claim to user documentation until that registry location
exists and a clean-environment installation has been verified.
