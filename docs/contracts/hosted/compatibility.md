# Compatibility policy

## Version selection

Contract names and positive integer versions form the compatibility key. A
consumer must select a schema from the current `index-v2.json` before
processing a payload. `index-v1.json` remains available as the preserved
legacy registry.
Unknown names and versions fail closed as `unsupported_contract_version`.
There is no implicit "latest" version and no content negotiation in this
package.

Version 1 objects are closed. Adding a required or optional property, changing
an enum, relaxing an authority invariant, or changing canonicalization is a
contract change that requires a reviewed new version. Documentation-only
clarifications that do not change validation or meaning may retain the version.

## Canonicalization and digests

All `*_digest` values are lowercase SHA-256 hex strings over the canonical
input named by the containing contract. Canonical JSON uses UTF-8, lexically
sorted object keys, no insignificant whitespace, and native JSON scalar forms.
Arrays retain their declared order unless the schema explicitly calls them a
set. Snapshot tree digests are derived from lexically sorted safe relative
paths and their file digests; timestamps and mutable labels are excluded.

Request payload digests exclude transport headers and include the complete
domain request body. Retrieval source-set digests include pinned campaign and
revision identifiers, authority, stable source identifiers, excerpt ordinals,
and excerpt digests. Proposal diff digests include the exact ordered structured
changes and visible authority transitions.

## Deferred work

Endpoint design, executable API and storage implementation, database
migrations, import/export, provider selection, and device persistence remain
non-authorized follow-up work. No implementation may infer those choices from
schema property names.
