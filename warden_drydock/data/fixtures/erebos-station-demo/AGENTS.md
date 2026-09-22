# Campaign Repository Agent Instructions

## Required startup sequence

1. Read this file.
2. Read `.drydock.json`.
3. Read `00-drydock/ai-context.md`.
4. Read relevant campaign entities and session records.
5. Classify the request before changing files.

## Request classes

- query
- idea capture
- draft creation
- canon proposal
- session preparation
- session recording
- world-state advancement
- audit
- framework maintenance

## Governing rules

- Git is the source of truth.
- Markdown is canonical storage.
- The human Warden approves canon.
- Never silently turn an idea into canon.
- Played events override preparation.
- Keep Warden truth, player knowledge, player belief, and public claims distinct.
- Make the smallest coherent edit.
- Do not expose Warden-only information in player-facing files.
- After material changes, run validation and rebuild AI context.
- Start discovery with the generated entity index and focused retrieval; do not load the whole campaign.
- Keep present truth under `## Current state` and preserve changes in approved session or event records.
- Put typed links only under `## Connections`, using ``- `relationship` → [[target-id|Readable Name]] (`current`) — Brief context.``
- Do not maintain inverse links manually; rebuild generated backlinks.
- Review legacy connection proposals before editing records. Proposals never promote ideas to canon.

## AI-assisted operation

The AI interprets the Warden's natural-language request, but uses deterministic repository tools when available.

```bash
python scripts/drydock.py validate
python scripts/drydock.py context
python scripts/drydock.py related ENTITY_ID
python scripts/drydock.py connections audit
python scripts/drydock.py new TYPE ID --name "NAME"
```

Do not rebuild repository structures from memory when a Drydock operation exists.
Use `new` for adapter-defined campaign records rather than copying templates by
hand. Newly created records are provisional; the canon gate still applies.

For framework updates, use `drydock upgrade .` to preview first. Use
`drydock upgrade . --apply` only after reviewing the preview. Never work around
an ownership conflict by overwriting campaign files.

## Canon gate

New material starts as `idea`, `draft`, or `review`. Promote it to `canon` only after explicit approval.
