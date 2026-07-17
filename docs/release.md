# Release Guide

## Prepare

1. Ensure the working tree is clean and all accepted decisions are reflected in
   documentation.
2. Set `warden_drydock.__version__`; package metadata and generated manifests
   read that value dynamically.
3. Update `CHANGELOG.md` with user-facing release notes.

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
python scripts/drydock.py new handout handout-smoke --name "Player Notice"
python scripts/drydock.py validate
python scripts/drydock.py context
```

Inspect the generated repository, `.drydock.json`, `.drydock-lock.json`, and AI
context. Confirm that packaged templates and adapter declarations are present.
For a handout smoke test, fill its audience before validation. Exercise at
least one representative record from every entity family and preview/apply an
upgrade against campaign-owned legacy content before tagging.

After the final PR merges, create the annotated release tag and install the
exact pinned GitHub ref in a fresh environment. Verify the raw tagged
`BOOTSTRAP.md`, reported version, bootstrap, standalone validation/context, and
explicit separation of Git initialization before advertising the release.

## Publish

Publishing is intentionally not automated in the MVP. A maintainer must review
the artifacts and explicitly upload them to the chosen registry. Do not add a
public installation claim to user documentation until that registry location
exists and a clean-environment installation has been verified.
