# Campaign Atlas HTTP contract family

`v1/index.json` is the entry point for the closed Campaign Atlas read contract.
It is separate from the transport-neutral hosted contracts and the general
hosted HTTP package. The v1 correction adds canonical GET serialization and
history direction without changing the projection query algorithms.

The package pins campaign discovery, overview, record library, detail,
depth-1 relationships, approved history, workflow summaries, and deterministic
cursor semantics. It defines routes for later backend work but implements no
handler. General hosted HTTP v2 owns campaign and record generation context;
Atlas exposes no separate generation request, response, or route.

## GET query serialization

Atlas reads use the canonical flat query parameters declared in
`v1/routes.json`. Each revision-bound read requires `revision_id`,
`revision_ordinal`, and `tree_digest`. Record filters repeat `type`, `authority`,
or `status`; comma-packed aliases are invalid. Paginated reads require an
explicit `limit` from 1 through 100. History accepts `direction=forward` or
`direction=backward`; omission means `forward`.

The parser rejects unknown parameters, duplicate singleton parameters,
malformed percent encoding, empty required values, aliases, invalid enums and
integers, and cursor reuse under another query binding. It canonicalizes each
repeated filter to sorted unique values before constructing the logical query.

Workflow HTTP is summary-only in this package. `atlas_workflow_summary` returns
persisted counts and the active session binding. It never returns proposal,
Draft, table-fact, or unresolved-question content.

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
