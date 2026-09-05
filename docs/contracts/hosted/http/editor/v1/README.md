# Record editor HTTP contract v1

This package defines the Warden-only record editor for the P6-RECORD-EDITOR
work. It is a public, versioned contract for the local browser pilot. The
editor accepts structured documents and returns immutable proposals. It does
not accept Markdown, paths, SQL, database IDs, provider objects, or campaign
content outside a request made by the Warden.

The prepared UX contract is the behavior reference:
[`docs/hosted-record-editor-ux.md`](../../../../../hosted-record-editor-ux.md).
This package closes the request, response, validation, diff, authority, and
recovery semantics that the UX artifact leaves open.

## Package files

- `index.json` identifies the package, its baseline, and its compatible parent
  contracts.
- `editor.schema.json` is the closed JSON Schema Draft 2020-12 document for
  views, requests, proposals, approval results, and errors.
- `routes.json` defines the versioned same-origin routes and binding rules.
- `examples.json` contains public synthetic payloads for every mutation kind,
  correction, rejection, removal impact, approval, replay, and historical read.
- `semantic-invariants.json` names the checks that JSON Schema cannot express.
- `negative/` contains fail-closed examples for each required rejection class.

## Contract shape

Every mutation uses an `editor_operation_request` with a request ID,
idempotency key, canonical payload digest, exact expected revision, and exact
campaign-wide `expected_editor_workflow_version`. A campaign has one editor
workflow counter. An accepted operation compares that counter and increments
it once. If the current counter is N, an accepted create, edit, remove, or
correction returns a proposal stamped N+1. Approval or rejection of that exact
proposal compares N+1 and completes at N+2. An exact replay returns its stored
result and does not increment the counter. Stale, invalid, and failed
operations also leave it unchanged.

Create, edit, remove, correction, rejection, and approval bind the exact base
revision. Edit and remove also bind the exact digest of every affected record.
Create binds a null record digest because the record does not exist in the base
revision. The server rejects a stale revision, stale record digest, or stale
workflow counter without merging or rebasing.
Approval or rejection against a changed head maps to HTTP 409 `stale_revision`.

The editor document has typed identity, adapter fields, adapter sections,
status, derived authority, explicit visibility metadata, the Warden-only flag,
and directed outgoing connections. A connection names a selected domain
target, relationship, state, context, and occurrence ID. The server derives
incoming backlinks from typed outgoing connections. The client cannot submit
reverse backlinks or server edge IDs.

Removal first returns an impact document. In v1 the impact set contains only
typed entries from `## Connections`. Each required reference must be removed,
redirected to a selected existing record, or explicitly accepted unresolved
when the validator marks that reference as permitted. The proposal binds the
impact digest so the resolution list cannot be applied to a different graph.
That exact binding and complete typed resolution set remain required on
correction, rejection, and approval; an approval cannot confirm a different
mutation. A removal correction repeats the exact impact binding and must
resolve the complete impact document again, with exactly one action for each
required reference. The proposal must contain exactly one matching resolution
card, affected-record binding, and derived graph effect for each impact
reference. Connection cards are derived from record before/after documents;
omissions, extras, no-ops, and directionally inconsistent effects fail closed.
Duplicate, missing, extra, self, and unknown redirect targets fail closed.

`editor_proposal_view.core_proposal` is `canon_proposal` v2, an additive
version of the existing proposal contract. It keeps proposal identity,
version, validation, source revision, base revision, change digest, and
approval meanings. Its `subject_id` accepts the existing Atlas/domain record
ID grammar directly, including hyphens and one-character IDs. The editor
extension adds structured cards and complete before/after data for creation,
removal, field and section changes, and any number of connection changes. There
is no legacy-ID mapping or translation table.

## Authority and visibility

Status and authority are separate. `canon` and `revealed` status produce the
matching authority. Every other supported status produces `preparation`.
Promotion to canon or revealed must be visible in a structured exact diff and
must be named in the approval confirmation. Approval alone never promotes a
record.

Visibility is explicit metadata, not authority. The document carries both an
audience (`warden`, `players`, or `shared`) and `warden_only`. Every visibility
change appears in the exact diff. A broadening requires explicit approval. The
server never widens visibility automatically and never infers Warden safety
from a name, status, relationship, or badge.

## Acceptance mapping for Issue #67

| #67 acceptance criterion | Contract evidence |
| --- | --- |
| Create, approve, and open the record in the new revision | `editor_record_create`, `editor_proposal_view`, and `editor_proposal_approve` routes. The create example is Draft first, then publishes one returned revision. |
| Edit prose, metadata, and connections without raw Markdown | `record_document.sections`, typed `fields`, `status`, `visibility`, `warden_only`, and `connections` in `editor_record_edit_request`. No Markdown field exists in the schema. |
| Correct a target and derive the incoming backlink | `connection_updated` cards contain the selected outgoing target and `derived_backlinks` are server-produced effects. No reverse connection request field is defined. |
| Remove only after affected references are resolved or explicitly accepted | `editor_removal_impact`, `impact_digest`, `reference`, and `resolution`. Approval requires the complete resolution set. |
| Exact before/after review covers every mutation | `editor_diff.cards` covers record create, update, remove, connection add/update/remove, and affected-reference resolution. Cards include typed before and after values. |
| Repeated submission is idempotent and stale edits cannot overwrite a newer head | `operation_request`, `editor_workflow_cas`, exact base and record bindings, and `editor_replay_digest`. The negative fixtures cover digest, workflow, and revision conflicts. |
| Approval publishes one validated immutable revision and preserves history | Approval requires passed validation and exact proposal bindings. `publication.published_revision` names one new revision. Removal is next-revision only. |
| No direct database or filesystem mutation bypasses typed services | Routes expose only record, proposal, correction, rejection, approval, and read operations. Public identifiers reject path-like values. There is no apply, SQL, filesystem, Git, or direct database route. |
| Contract, PostgreSQL, browser, accessibility, stale-head, replay, create/edit/remove, and restart/readback tests pass | The test obligations below bind these checks to this package. Persistence and browser implementations must consume these fixtures and preserve the route semantics. |

## Protected invariants

- Historical revisions are immutable and readable. Only the current head is
  writable.
- The current head changes only through exact approval of a validated proposal.
- Draft and proposal authority never becomes record canon by display or by
  provider output.
- Record IDs remain stable through ordinary edits. Displayed names may change.
- Status derives record authority. `canon` and `revealed` map to matching
  authority; every other supported status maps to preparation.
- Proposal changes, cards, transition entries, references, resolutions, and
  record bindings are unique by logical ID, even when duplicate objects differ.
- Connections are directed and explicit. Backlinks are derived and never
  edited as duplicate records.
- Removal never cascades silently and never erases historical records.
- A correction creates a new immutable proposal version. It never edits the
  prior version.
- The workflow CAS counter is campaign-wide, monotonic, and incremented once
  per accepted non-replay mutation.
- Authority promotions and visibility changes are visible in the exact diff
  and require explicit Warden approval. Visibility never widens by default.
- v1 affected-reference analysis is limited to typed `## Connections`.
- Binding, validation, replay, and publication failures leave the prior head
  unchanged and return closed, public-safe errors.
- Provider setup, consent, and outage do not block manual editing, validation,
  review, correction, rejection, or approval.
- Rejection is explicit from Draft or NeedsReview and preserves the prior head.
- No contract value contains campaign secrets, provider output, private notes,
  filesystem paths, database identifiers, or raw campaign data in these
  synthetic examples.

## Compatibility and migration impact

This is an additive package at a new versioned path. It does not modify the
existing HTTP v1 or v2 files, the Atlas package, or persistence schemas. The
editor uses `canon_proposal` v2 as a compatible extension of the existing v1
proposal contract. Existing proposal consumers can continue handling v1;
editor-aware consumers must accept Atlas/domain IDs directly and understand
the `editor_diff` extension before advertising create, remove, or
multi-connection support.

The proposal service must persist `proposal_id` and `proposal_version` as
immutable references, preserve `base_revision` and all record digests, and
store the editor payload and diff digests with the existing receipt. The
campaign persistence layer needs one editor workflow counter and an atomic
compare-and-swap operation. This package does not prescribe a migration or
change campaign data. A later implementation package owns the database
migration, backfill default, and restart recovery tests.

Approval continues to use the existing revision and publication-intent
authority boundary. The editor service may construct and validate a candidate,
but it cannot publish a snapshot directly.

## Test obligations

Contract tests must:

- validate every example against `editor.schema.json`, validate the schema
  itself, and reject every negative fixture for its declared category;
- check canonical payload, proposal, diff, validation, record, and impact
  digests, including array order and exact text normalization rules;
- verify path/body equality, direct hyphenated Atlas/domain IDs, safe public
  IDs, no path interpretation or legacy-ID translation, and closed errors
  without free-form details;
- exercise create, prose and metadata edit, rename with stable ID, one and
  many connection changes, target correction, removal impact, all resolution
  choices, and derived backlink output;
- assert authority and visibility changes are explicit in both the diff and
  approval request, and that an unconfirmed transition cannot publish;
- assert exact replay returns the original response, a changed payload returns
  `replay_mismatch`, and a stale editor workflow counter returns
  `workflow_conflict` without mutation;
- assert stale base revisions and record digests preserve the proposal and
  head, with no silent merge or rebase;
- assert correction creates a new version and cannot change proposal
  provenance, widen its change set, or overwrite the prior version;
- assert invalid, rejected, unconfirmed, and failed proposals publish nothing;
- run persistence restart/readback checks for receipts, proposal versions,
  workflow counter, impact digest, and publication intent;
- run browser checks for current-head versus historical read-only behavior,
  keyboard and focus handling, error preservation, provider-unavailable
  manual paths, and 320 CSS pixel layout; and
- run PostgreSQL and revision-service checks proving the head advances once and
  historical revisions remain readable.

The implementation work package must also run the repository checks listed in
the parent instructions. This documentation package itself makes no runtime
or campaign-content change.
