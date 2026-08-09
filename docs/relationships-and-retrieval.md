# Relationships and focused AI retrieval

Markdown entity records are canonical. A connection is a directed, typed aid to navigation, not a replacement for prose, current state, beliefs, or session history.

```markdown
## Connections

- `works-for` → [[faction-company|The Company]] (`current`) — Handles salvage contracts.
```

Only this syntax beneath `## Connections` is parsed. Targets use stable IDs; aliases are presentation only. Inverse links are generated. Campaign records are never rewritten when indexes are built.

Run `python scripts/drydock.py index` after edits. Use `find`, `show`, `related`, `backlinks`, and `history` for progressive retrieval. `context --focus ID --depth 1 --max-records 20` creates a bounded neighborhood and reports omissions.

Existing campaigns remain valid without connection sections. `connections audit` reports explicit legacy frontmatter as review-only proposals and never changes files or canon. Framework upgrades update only unmodified managed/shared assets; campaign-owned entity files remain untouched.
