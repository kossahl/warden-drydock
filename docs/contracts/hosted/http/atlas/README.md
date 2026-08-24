# Campaign Atlas HTTP contract family

`v1/index.json` is the entry point for the closed Campaign Atlas read contract.
It is separate from both accepted hosted v1 packages. Existing contract names,
versions, schemas, examples, and routes keep their original meaning.

The package pins campaign discovery, overview, record library, detail,
depth-1 relationships, approved history, workflow summaries, contextual AI
focus, and deterministic cursor semantics. It defines routes for later backend
work but implements no handler.

## Projection and authority boundary

Immutable verified snapshots remain authoritative. Migration `0006` adds
revision-keyed PostgreSQL rows for Atlas records, normalized content, edge
occurrences, and approved-history changes. Normalized content is cached because
casefold search and exact record reads need a deterministic revision-local
representation. Its digest binds the returned text. The snapshot manifest's
file-byte digest remains internal and distinct.

The projection stores raw status and verifies its derived authority. Neither a
caller nor PostgreSQL may set source authority independently. Workflow rows,
audit events, Drafts, table facts, and proposal lifecycle changes do not enter
approved history. Nullable proposal identifiers are enrichment from an exact
published-revision lookup and disappear during snapshot-only recovery.

## Rebuild, rollback, and empty-system recovery

Build and validate a complete revision bundle before opening its replacement
transaction. Replacement locks and deletes only one campaign and revision,
inserts its rows, checks counts and the projection digest, then commits. A
failure rolls back to the previous verified rows. Newer revisions never delete
older ones.

For an empty database, verify the unique campaign lineage and rebuild each
eligible revision in ordinal order. Rebuilding revision N stores its record and
edge state plus N's history delta. Earlier revisions must also be rebuilt to
serve the complete history through N.

Migration `0006` is additive. Rolling back to an application image that expects
`0005` leaves the new tables unused. Do not delete pilot data to reverse it. If
schema removal ever becomes necessary, restore a verified backup into fresh
volumes or recover Atlas rows from verified snapshots while retaining the old
volumes as the rollback target.
