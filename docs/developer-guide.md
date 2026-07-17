# Warden Drydock Developer Guide

## Required architecture context

Before product or architectural changes, read `AGENTS.md`, the project brief,
product decisions, conversation handoff, and every accepted ADR. The governing
split is: the AI interprets intent; deterministic Drydock code mutates the
repository.

## Architecture

- `warden_drydock/cli.py` exposes framework commands.
- `warden_drydock/core/generator.py` composes the generic project template and
  selected adapter into a standalone campaign.
- `warden_drydock/standalone.py` is standard-library-only portable maintenance
  code. The framework imports it and the generator copies it verbatim to
  `scripts/drydock.py` in each campaign.
- `warden_drydock/core/upgrade.py` compares ownership locks and performs
  preview-first, conflict-safe managed updates.
- `warden_drydock/data/project_template/` contains system-agnostic campaign
  assets.
- `warden_drydock/data/adapters/` contains system-specific declarations,
  guidance, and templates.

Generated campaigns are runtime-independent artifacts. Never add imports from
the framework package to `standalone.py`.

## Ownership model

Generation writes `.drydock-lock.json` with a schema version, installed
versions, ownership, and SHA-256 baseline for every supplied file.

- `framework`: maintained by Drydock while locally unmodified.
- `adapter`: system-specific managed asset.
- `shared`: an upstream default that may be customized; conflicting upstream
  changes require explicit resolution.
- `campaign`: user content, never overwritten by upgrade.
- `generated`: rebuilt cache, never handled as source content by upgrade.

Upgrade planning is all-or-nothing when conflicts exist. Preview is the default;
writing requires `--apply`.

## Entity and adapter model

The portable engine reads `00-drydock/adapter.json` from the campaign. The
adapter declares entity names, source templates, destination patterns, and
required frontmatter fields. This powers both `new` and semantic validation,
keeping Mothership concepts outside the generic core.

See [adapter development](adapter-development.md) before adding or changing an
adapter.

## Local development

Warden Drydock requires Python 3.11 or newer and has no runtime dependencies.

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m warden_drydock --help
```

When generator, template, adapter, or portable maintenance behavior changes,
generate a fresh campaign in a temporary sibling directory, inspect it, run its
local validation/context commands, and verify that it does not import the
framework source checkout.

## Change checklist

1. Add negative-path tests before or with the behavior change.
2. Preserve campaign content and the canon gate.
3. Keep portable maintenance code standard-library-only.
4. Update user-facing and generated instructions when commands change.
5. Run the complete test suite and CLI help check.
6. Build and smoke-test the wheel for release-affecting changes.
7. Review `git diff --check` and commit one coherent task.

For artifact and clean-install verification, follow the [release guide](release.md).
