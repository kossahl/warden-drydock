# Hosted record editor UX contract

Status: design artifact for P6-RECORD-EDITOR-CONTRACT v1

Scope: the Warden-only browser editor described by [P6-RECORD-EDITOR
v2](https://github.com/kossahl/warden-drydock/issues/67). This document defines
observable UI behavior. It does not add an API schema, choose endpoint names, or
define a storage implementation.

## 1. Contract basis

This artifact follows:

- [Authoritative Product Decisions](product-decisions.md), especially the
  canon gate, authority layers, and local browser pilot boundaries.
- [ADR-004](adr/004-hosted-engine-api-boundary.md), which makes the browser a
  client of a versioned domain API and keeps proposal approval above the
  deterministic engine.
- [ADR-005](adr/005-hosted-snapshot-workflow-storage.md), which makes approved
  campaign revisions immutable and requires exact proposal approval, explicit
  correction, and fail-closed stale-head handling.
- [ADR-006](adr/006-local-compose-security-operations.md), which keeps the
  pilot local, Warden-operated, and provider credentials out of browser state.
- [relationships-and-retrieval](relationships-and-retrieval.md), which defines
  the explicit outgoing connection syntax and derived backlinks.
- The current [hosted contract index](contracts/hosted/index-v2.json),
  [authority and redaction policy](contracts/hosted/authority-redaction.md),
  and [existing browser UX reference](../web/src/ProposalWorkspace.tsx).

The implementation must use the accepted public contract family and version
selected by the parent work package. This UX artifact deliberately does not
invent editor request or response payloads.

## 2. Terms and boundaries

| Term | UI meaning |
| --- | --- |
| Current head | The campaign revision that accepts the next approved publication. The record editor is writable only when its viewed revision is this head. |
| Historical revision | Any approved revision other than the current head. It remains readable and immutable. |
| Record ID | The stable domain identifier. It is visible in an advanced details area and is never changed by ordinary editing. |
| Displayed name | The editable human-readable name. Renaming does not change the Record ID. |
| Status | The record's stored lifecycle value, such as `idea`, `draft`, `review`, `canon`, `revealed`, or `archived`. The UI shows unknown or missing values as validation states rather than silently normalizing them. |
| Authority | A derived read label. `canon` and `revealed` status produce the corresponding authority; all other supported record statuses remain preparation authority. A proposal or generated Draft is workflow authority, not record authority. |
| Visibility | The adapter-defined audience value. For the Mothership adapter this is `warden`, `players`, or `shared`. Visibility is not authority. |
| Warden-only | The adapter's explicit protection flag. The UI must not infer safety from a name, status, or visibility label. |
| Proposal | An immutable, reviewable candidate change. It is not canon and does not change the current head until exact approval succeeds. |
| Affected reference | A deterministic reference that must be removed, redirected, or explicitly accepted as unresolved before a removal can be approved. |

The Warden is the only editor role in this milestone. There is no player editor,
player approval, shared editing, or player account surface. A future player view
must apply the visibility and Warden-only boundary independently. The editor
must never use a player-visible record as a reason to reveal Warden-only target
content.

## 3. Information architecture

The editor opens from a current-head record in Campaign Atlas. The normal page
order is:

1. Campaign and revision context.
2. Record identity and authority summary.
3. Basics and adapter-defined metadata.
4. Adapter-defined content sections.
5. Outgoing connections.
6. Removal impact, when removing.
7. Validation summary.
8. Exact proposal review and approval.

The history, Atlas Relationships view, and exact source Markdown remain
available as inspection routes. Raw Markdown is an inspection aid, not the
normal editing surface. Incoming backlinks are shown in the Relationships
view and in removal impact review. They are never edited as duplicate reverse
entries.

### Desktop wireframe

```text
┌ Campaign name                         Viewed revision 12 · Head · Warden ┐
│ Record name                  Preparation  ·  warden visibility             │
│ Record type · stable ID (advanced)                         [History]      │
├───────────────────────────────┬───────────────────────────────────────────┤
│ Edit record                    │ Review panel                              │
│ Basics                         │ No changes yet                            │
│ Content sections               │ or                                         │
│ Connections                    │ 3 changes · validation passed             │
│ Removal impact, if applicable  │ [Review exact changes]                    │
│                               │                                           │
│ [Cancel] [Save as proposal]   │                                           │
└───────────────────────────────┴───────────────────────────────────────────┘
```

The review panel is a summary only. The exact diff is a full-width review mode
or a clearly labelled lower section. Approval must not be hidden in a narrow
sidebar.

### 320 CSS pixel wireframe

```text
Campaign name
Viewed revision 12 · Head
Record name
Preparation · Warden only

Basics                       [open]
Content                     [open]
Connections                 [open]
Removal impact              [open, when needed]

Validation summary
[Review exact changes]

[Save as proposal]
```

At 320 CSS pixels, sections stack in one column. The implementation must not
require horizontal scrolling for fields, controls, validation findings, or the
exact diff. A long stable ID or digest may wrap in an advanced details block.

## 4. Revision mode and entry states

Every record page has a persistent context strip with campaign name, viewed
revision ordinal and identifier, head marker, record name, record type, and
record authority. The strip is text, not color alone.

### Current head

When the viewed revision equals the current head:

- `Edit record`, `Remove record`, and `Create record` actions are available to
  the Warden.
- The form starts from the loaded record content and its bound record digest.
- The page states that changes are proposed and will not change the head until
  approval.
- A head refresh or submit always rechecks the current head. The browser must
  not assume that the page remained current.

### Historical revision

When the viewed revision is historical:

- The record, metadata, connections, backlinks, source Markdown, and history
  remain readable.
- Editing controls are absent or disabled with a text explanation: "Historical
  revisions are read-only. Open the current head to propose a change."
- A single prominent `Open current head` action preserves the record ID and
  navigates to the head record. It does not copy or submit edits.
- Removal and create actions are unavailable in this mode.
- Any Draft or proposal opened from historical context is visibly bound to its
  historical source. It cannot create a current-head proposal until the Warden
  opens the head and starts again.

No historical page may show a control that appears to mutate the historical
snapshot.

### Loading, unavailable, and integrity states

- On first load, show a labelled busy state and keep the page structure stable.
- On not found, show the record or revision was not found and provide a route
  back to Atlas.
- On integrity or lineage failure, show a blocking error. Hide mutation actions
  and do not offer a retry that could publish.
- On a transient read failure, keep no stale editable form. Offer `Retry` and
  preserve unsent input only if the browser can prove which revision the input
  belonged to.

## 5. Create flow

1. The Warden selects `Create record` from the current-head Atlas view.
2. The type picker lists only adapter-supported record types. Each option shows
   its plain-language label and a short description when the adapter provides
   one.
3. After type selection, the form renders the adapter-supported fields and
   sections for that type. It does not render a generic arbitrary Markdown
   editor.
4. The form allocates a candidate stable ID from the Warden's entered name or
   another deterministic UI suggestion. The Warden can edit the candidate
   before submission, subject to the domain identifier rules. The server owns
   final allocation and validation. The UI never accepts a path.
5. The form shows the adapter's provisional defaults. For the current
   Mothership templates, that means the template's provisional status and
   Warden-only visibility defaults. The form must not silently choose canon or
   revealed authority.
6. `Save as proposal` validates the entered record and creates a Draft proposal
   bound to the current head. It does not create a head or canon record.
7. The Warden reviews the exact creation diff. The review includes the new
   identity, all metadata, each non-empty section, connections, visibility, and
   authority outcome. The new record is marked `added`, with no "before" record.
8. After complete candidate validation, `Approve and publish exact proposal`
   opens explicit approval confirmation. On success, the UI opens the new
   record in the returned approved revision and marks it as the current head.

Duplicate ID, unsupported type, required-field, adapter, or validation errors
return to the form with the Warden's content intact. The first invalid field
receives focus after the error summary is announced.

## 6. Edit flow

1. The Warden selects `Edit record` at the current head.
2. The form displays identity, metadata, sections, and outgoing connections in
   grouped sections. It loads current values, not values from a previous
   proposal or history view.
3. The Record ID and record type are read-only in this flow. The displayed name
   is editable and its rename is shown as a metadata change. A type migration
   is outside this work package.
4. The Warden edits one or more supported fields or sections. A section heading
   is fixed by the adapter. Optional adapter sections may be left empty. The UI
   must not invent headings or silently delete content from an unsupported
   section.
5. The Warden may change status, visibility, Warden-only state, and other
   adapter-supported metadata. Authority is shown as a computed consequence.
6. `Save as proposal` validates the complete candidate revision and opens the
   review state. It does not update the record page behind the review.
7. The exact review lists every changed record property and section, including
   unchanged context needed to identify the field. A correction produces a new
   immutable proposal version.
8. Approval requires the Warden to confirm the displayed proposal version,
   base revision, affected record count, authority transitions, and validation
   result. A successful approval opens the returned new immutable revision.

The form must preserve entered text when a field fails validation. It must not
replace a Warden's value with a server-normalized value without showing that
value in the review.

## 7. Metadata, status, visibility, and authority

### Metadata editing

The Basics section contains:

- displayed name;
- stable Record ID, read-only after creation;
- record type, read-only after creation;
- adapter-supported status;
- adapter-supported visibility;
- adapter-supported Warden-only flag;
- adapter-required fields such as date or audience, when applicable; and
- a collapsed `Source details` area containing viewed revision and digest
  bindings for inspection.

The form labels required fields, allowed values, and any adapter-specific
rules. It does not expose filesystem paths, SQL identifiers, internal handles,
provider-native values, or raw database data.

### Status and authority behavior

Status and authority are separate rows in the UI:

```text
Status       [review ▼]
Authority    Preparation (derived from status)
```

The UI uses these rules:

- `idea`, `draft`, `review`, `archived`, `accepted`, missing, and unknown
  status values do not become canon authority automatically.
- `canon` and `revealed` show the matching authority.
- A transition to `canon` or `revealed` is a visible structured change in the
  exact diff. Approval alone does not imply promotion.
- An authority transition is never hidden in prose or represented only by a
  color badge.
- The editor does not offer a separate "make canon" shortcut that bypasses
  status validation and exact review.

### Visibility and Warden-only behavior

Visibility is an audience label, not permission to edit. The current Mothership
values are `warden`, `players`, and `shared`. The editor must:

- show the current visibility and Warden-only state together;
- show the adapter validation warning before review when a combination is
  forbidden;
- show a prominent warning before approving a change that broadens audience;
- require the handout audience when the adapter requires it; and
- keep Warden-only content out of any player-facing response or preview.

The editor may show full Warden content because this is a Warden-only surface.
It must not add a player preview that can be mistaken for a permission check.

## 8. Field and section editing

Each adapter-supported field uses the simplest suitable control:

| Value | Control behavior |
| --- | --- |
| Single-line text | Labelled input with length and required state. Long values wrap in review. |
| Multi-line prose | Labelled textarea. Preserve line breaks and entered content. Show a plain-text preview and exact source inspection in review. |
| Status, visibility, authority-related values | Select or radio group with the current value, allowed values, and a text explanation of the resulting authority. |
| Boolean Warden-only state | Checkbox with an explicit label. Do not encode it only as a badge. |
| Date or adapter-defined scalar | Labelled control with the adapter's validation message. Do not infer a date from the browser locale. |
| Adapter-defined optional section | Collapsible section. Empty optional sections remain empty and are not silently removed. |
| Adapter-defined required section | Expanded when invalid or incomplete. The error names the section and preserves its contents. |

Section headings come from the adapter record definition. The Warden edits
section bodies, not arbitrary heading names. This keeps the output compatible
with adapter validation and prevents a UI-only section from becoming campaign
content.

The `Connections` section is a structured editor described below. The exact
source Markdown remains inspectable in a separate disclosure with a warning
that it is read-only inspection.

## 9. Connection editing

Connections are directed, explicit outgoing records. Incoming backlinks are
derived from the graph and are not duplicate entries.

### Outgoing connection list

Each outgoing row shows:

```text
Target:       The Company          [Change target]
Relationship: works-for            [select]
State:        current               [select]
Context:      Handles salvage contracts. [edit]
                                      [Remove connection]
```

The target picker searches and selects an existing record by displayed name,
record type, and stable ID in the secondary text. It never accepts an arbitrary
path or free-form target ID as a substitute for selection. The target record's
visibility is shown when it affects validation, but its Warden-only content is
not shown inline.

The relationship and state choices come from the adapter vocabulary. The
context sentence is required by the accepted connection syntax and is edited as
plain text. A connection row has a stable UI key while editing so validation
can identify the specific failed row. The browser must not deduplicate or
rewrite rows without showing the resulting exact diff.

When the Warden changes a target, the review shows one outgoing target change.
The approved projection then derives the matching incoming backlink. The UI
must not ask the Warden to create the reverse entry. If the target is removed,
the incoming reference appears in removal impact review instead.

Connection validation must visibly cover:

- missing or unavailable target;
- unsupported relationship or state;
- empty context;
- duplicate occurrences;
- player-visible source pointing to a Warden-only target; and
- any adapter-specific connection rule.

The error stays attached to the row and preserves all entered values.

## 10. Remove flow and affected-reference resolution

Removal is a candidate change to the next revision. It never deletes the record
from a historical revision and never cascades silently.

1. The Warden selects `Remove record` from the current-head record page.
2. The editor opens an impact review before it creates a removal proposal. The
   review names the record and shows its stable ID, type, displayed name,
   authority, visibility, outgoing connections, incoming backlinks, and every
   affected reference returned by deterministic analysis.
3. Each affected reference has a required resolution control:

   - `Remove reference`, which removes the referencing connection or supported
     reference from the candidate.
   - `Redirect`, which opens the existing-record picker and replaces the target
     with the selected record.
   - `Accept unresolved`, available only when validation marks that reference as
     permitted unresolved. The row remains visible in the review and the
     unresolved result is explicit.

4. The Warden cannot continue while a required reference has no resolution.
   The UI does not guess between removal and redirection.
5. The resulting review shows the removed record, every resolved reference,
   every remaining permitted unresolved reference, and the resulting outgoing
   and derived incoming graph changes.
6. The proposal binds the exact impact digest, impact record binding, and
   complete typed resolution set. Correction, rejection, and approval repeat
   that binding; the Warden cannot confirm a different graph mutation.
7. `Approve and publish exact removal` opens a destructive confirmation. The
   confirmation says that the record disappears only from the new approved
   revision, historical revisions retain it, and the listed references will
   change. The Warden checks an explicit acknowledgement before approval.
8. On success, the UI opens the new revision's Atlas view. It must not navigate
   to a nonexistent removed record page.

The removal review is also the place where the Warden can cancel safely. Cancel
leaves the current head and all historical revisions unchanged.

## 11. Proposal review and exact diff

All mutation kinds use one review language. The backend contract may represent
the data differently, but the browser must receive enough typed information to
render the following complete review without reconstructing meaning from raw
Markdown.

### Change cards

The exact diff contains one ordered card per changed subject and clearly labels
the mutation kind:

| Mutation | Required review content |
| --- | --- |
| Create | `Added`, new Record ID, type, displayed name, all metadata, authority outcome, each populated section, and each outgoing connection. |
| Edit | `Updated`, Record ID, before and after displayed name, metadata rows, authority transition if any, and before/after content for every changed section. |
| Connection add | Source and target names plus stable IDs in details, relationship, state, and context before `none` and after the entered values. |
| Connection update | Same source and target, with each changed relationship, state, context, or target shown as before and after. Derived backlink effect is labelled as derived. |
| Connection remove | The exact outgoing row that will disappear, with source, target, relationship, state, and context. |
| Remove | `Removed`, the complete before identity and content, no after record, plus every reference resolution and graph effect. |
| Affected-reference resolution | Referencing record, reference kind and context, original target, selected action, replacement target when redirected, and unresolved marker when accepted. |

For content, use a readable section-level before/after view with changed lines
or paragraphs marked by text and position. A complete before/after source view
must remain available for exact inspection. Deletions cannot be hidden behind a
summary such as "content changed". Metadata and connections cannot be omitted
because the prose is unchanged.

The review header shows:

- proposal status and immutable proposal version;
- viewed/source revision and base revision;
- affected record count;
- validation status and all findings;
- authority transitions;
- a human-readable change summary; and
- an advanced disclosure for stable IDs and digests.

The proposal is labelled `Draft` or `Proposal`, never `Canon`. A proposal is
`not_published` until its status is `approved`; `Draft`, `NeedsReview`, and
`rejected` states never carry a published revision. If any validation finding
is unresolved, approval is unavailable. `Reject proposal` is explicit, returns
a rejection result, and leaves the current head unchanged.

### Approval confirmation

The approval dialog must include the exact proposal version, base revision,
affected record names, removal count, audience broadening, authority
transitions, and validation result. The primary action says `Approve and
publish exact proposal`. It is disabled until the Warden checks the explicit
confirmation. Escape and Cancel close the dialog without changing state.

Approval publishes one validated immutable revision or no revision. On success,
the UI announces the new revision and opens it. It does not show a local
optimistic canon state before the server result.

An exact replay returns the stored response at the same workflow version. A
correction advances the proposal workflow once, and its rejection or approval
advances it once more; stale, rejected, invalid, and replayed operations do not
increment it.

## 12. Validation, stale, conflict, and correction states

### Validation

Validation runs against the complete candidate. Findings appear in a summary
at the top of review and beside the affected field, section, or connection row.
Each finding has a plain-language message, a target location, and a recovery
action where one exists. The form keeps the Warden's entered content.

Validation must cover the existing work-package categories: frontmatter and
adapter schema, required fields and unique IDs, supported types and metadata,
connection targets and duplicate occurrences, permitted unresolved references,
authority and visibility transitions, base revision and record digest, and
candidate snapshot integrity before publication.

### Stale base and conflict

If the current head changes after a form or proposal was opened, show a blocking
banner:

```text
This proposal is based on revision 12. The current head is revision 13.
Nothing was published. Your proposal is preserved.

[Compare with current head] [Start correction from revision 13]
```

The UI must not silently merge or rebase. `Start correction from current head`
opens a new editable candidate from the current head and lets the Warden
manually reapply the intended changes. The old proposal remains inspectable.
`Compare with current head` shows the proposal base, current head, and proposed
after state without treating any side as selected.

Approval of a stale proposal is unavailable. A server conflict response changes
the proposal state to `Conflict`, preserves the exact proposal, and exposes the
same correction path. The Warden must explicitly choose the current-head base.

### Correction and replay

Correction never edits an existing proposal version. Submitting a correction
creates a new immutable version with a new exact diff and validation result.
The review keeps a link to the prior version and says which version is active.

Repeated submission of the same user action is idempotent. While a request is
pending, disable the relevant action and show its in-progress label. After a
network interruption, retry the same action or reload its persisted result.
Do not create a second proposal merely because the browser timed out. A changed
payload under a reused operation identity is shown as an idempotency conflict
and requires a new explicit action.

## 13. Provider-unavailable behavior

The editor is deterministic and remains usable when the provider is not ready.

| Provider state | UI behavior |
| --- | --- |
| Setup required | Show `Provider setup required` in the global status area. Hide or disable AI controls. Keep Atlas reads, manual editing, validation, proposal review, and approval available. |
| Consent required | Explain that grounded AI is optional and offer the existing consent action. Manual editing and all deterministic review paths remain available. |
| Unavailable or outage | Show `Provider unavailable`. Do not retry inference automatically. Keep manual editing, validation, correction, rejection, and approval available. |
| Retryable generation failure | Preserve the persisted Draft and its source binding. Offer resume or a new explicit request according to the existing generation state. Do not turn a transport retry into a mutation. |
| Terminal generation failure | Show the failure as no published Draft or proposal. Manual editing remains available. A new inference requires a new explicit request. |

No provider prompt, generated content, source excerpt, credential, or secret
enters the editor's URL, browser storage, logs, or public repository.

## 14. Keyboard and accessibility contract

- Use one `main` landmark, a labelled form, an `h1` for the record, and
  ordered `h2` sections. Provide a skip link to the form or main content.
- Every input has a visible label. Required state, allowed values, and errors
  are programmatically associated with the control.
- Use native inputs, buttons, selects, fieldsets, legends, and details where
  they fit. No drag-and-drop interaction is required for connections or
  sections.
- The review diff is a labelled region. Before and after columns have headings;
  change meaning is available in text and does not depend on color.
- Validation summary appears before the form fields in reading order. On
  submit, focus the summary or first invalid control and preserve the user's
  content. Errors use `role=alert` or an equivalent live announcement without
  repeating the whole form.
- Async status, save state, conflict, and approval result use a polite live
  region. Do not announce every typed character.
- Dialogs have a labelled title, move focus into the dialog, trap focus while
  open, close on Escape, return focus to the invoking control, and prevent
  background activation.
- All actions are keyboard reachable in a logical order. A visible focus
  indicator remains present. Do not use hover-only controls or icon-only
  buttons without accessible names.
- Target touch controls at least 44 CSS pixels in the primary action area.
  Keep destructive and approval actions separated from Cancel.
- Meet the repository's browser accessibility test conventions for contrast,
  reduced motion, semantic roles, and no color-only status. Respect
  `prefers-reduced-motion`.
- Long names, IDs, digests, prose, validation messages, and connection context
  wrap rather than clip. Code-like values may use horizontal scrolling only in
  their explicitly labelled inspection block.

## 15. Responsive behavior at 320 CSS pixels

The 320 CSS pixel viewport is a supported minimum, not a special error state.

- Use a single column. Do not keep a desktop sidebar beside the form.
- Keep the revision and authority summary at the top, with labels wrapping onto
  multiple lines.
- Stack form labels, controls, and help text. Inputs use the available width
  and never force a fixed minimum wider than the viewport.
- Make section disclosures full-width and keyboard operable. Preserve open
  state when validation returns to the form.
- Stack before and after diff panels vertically, with `Before` immediately
  followed by `After` for each change card.
- Stack action buttons in this order: secondary Cancel, correction or reject,
  then primary save or approve. Use full-width buttons for important actions.
- Keep the destructive confirmation summary readable before the acknowledgement
  checkbox. Do not require a side-by-side comparison.
- Allow the review and validation summary to scroll with the page. Do not make
  a nested horizontal scroller the only way to discover a finding.
- Test long translated-like labels and the longest supported connection context
  in addition to short fixture text.

## 16. Implementation constraints and observable checks

The frontend implementer should be able to test the following without reading
implementation internals:

1. Opening a head record exposes create, edit, and remove actions. Opening a
   historical record exposes none of them and offers `Open current head`.
2. Creating a supported record shows adapter fields, produces a Draft proposal,
   shows an `Added` exact diff, and opens the approved new revision only after
   explicit approval.
3. Editing a displayed name keeps the same Record ID. Editing prose and
   metadata shows each changed section or field in before/after review.
4. Changing a connection target through selection changes the outgoing row and
   the approved graph derives the expected incoming backlink without a reverse
   edit.
5. Removing a record opens impact review. Approval is unavailable until each
   affected reference is removed, redirected, or explicitly accepted when
   permitted. No historical read disappears.
6. A wrong, missing, duplicate, or forbidden connection target returns an
   attached error and preserves entered values.
7. A proposal displays its exact base revision and record digest binding, and a
   changed head produces a visible conflict with explicit correction from the
   new head. No silent merge occurs.
8. Repeating a submitted action returns the same result. Reusing its operation
   identity with changed content shows an idempotency conflict.
9. Approval of a valid exact proposal publishes one validated immutable revision;
   approval of a stale, invalid, rejected, or unconfirmed proposal publishes
   nothing.
10. Provider setup, consent, and outage states disable only provider-dependent
    controls. Manual editing, validation, review, and approval remain usable.
11. Keyboard-only operation reaches every control, reports errors and async
    outcomes, and operates dialogs without focus loss.
12. At 320 CSS pixels, all required controls and diff meaning remain visible,
    readable, and operable without horizontal page scrolling.

The browser may inspect the exact source Markdown, but no editor test should
require the Warden to type raw Markdown or a filesystem path.

## 17. Acceptance mapping for Issue #67

| Issue #67 acceptance criterion | UX contract location and observable result |
| --- | --- |
| Warden can create a record, approve it, and open it in the new revision. | Section 5 and checks 1, 2, 9. Create is current-head only, Draft first, exact review, explicit approval, then open the returned approved revision. |
| Warden can edit prose, metadata, and connections without touching raw Markdown. | Sections 6 to 9 and check 3. Adapter fields and sections are normal controls; raw source is inspection-only. |
| Correcting a wrong connection target produces the expected outgoing link and derived incoming backlink. | Section 9 and check 4. Target selection edits only the explicit outgoing row; backlinks are derived. |
| Warden can remove a record only after resolving or explicitly accepting affected references. | Section 10 and check 5. Required reference actions block review until resolved or validator-permitted unresolved. |
| Exact before/after review covers every mutation. | Section 11 and check 3. Create, field, section, metadata, authority, connection, affected-reference, and removal changes each have a card shape. |
| Repeated submission is idempotent; stale edits cannot overwrite a newer head. | Section 12 and checks 7 to 9. Same action replays its result; stale approval is blocked and preserved. |
| Approval publishes one validated immutable revision and preserves all historical reads. | Sections 5, 6, 10, and 11 plus check 9. Approval is explicit, validated, compare-and-swap bound, and followed by head navigation. |
| No direct database or filesystem mutation route bypasses typed proposal and revision services. | Sections 1, 2, 5, 11, and 16. The UI accepts domain selections and typed values only, never paths, SQL, or direct apply operations. |
| Contract, PostgreSQL, browser, accessibility, stale-head, replay, create/edit/remove, and restart/readback tests pass. | Sections 12, 14, 15, and 16. The listed UI states and checks are the browser-facing evidence; backend and persistence checks remain owned by their implementers. |

## 18. Protected invariants

- Historical revisions are immutable and remain readable.
- The current head changes only through exact approval of a validated proposal.
- Draft, proposal, and generated content never become canon by display or by
  provider output.
- Authority promotion is visible and follows status validation.
- Record IDs are stable through ordinary edits. Displayed names may change.
- Connections are directed and explicit. Incoming backlinks are derived.
- Removal never silently cascades and never erases history.
- Public UI values are domain identifiers and adapter vocabulary, not paths,
  filesystem handles, SQL identifiers, or provider-native objects.
- Provider failure does not block deterministic editing or review.
- Warden-only content cannot leak into a player-facing surface.

## 19. Non-goals

This artifact does not define or authorize:

- player editing or player approval;
- simultaneous collaboration or remote multi-user authorization;
- live cockpit behavior or offline capture;
- import, export, bulk editing, or type migration;
- automatic merge or automatic rebase after a stale head;
- autonomous AI approval or AI-created canon;
- arbitrary Markdown, filesystem, Git, SQL, or database editing;
- provider selection, provider configuration, or secret handling;
- endpoint names, request JSON, response JSON, database migrations, or
  production implementation details; or
- changes to campaign content.

## 20. Settled product choices

Issue #212 authorizes these choices for this contract and the #67 editor work:

1. Affected references are limited to typed `## Connections` for v1. The
   deterministic impact response is complete for that syntax; no broader
   Markdown reference scan is implied.
2. Editor mutations use one campaign-wide editor workflow compare-and-swap
   counter. Accepted non-replay mutations increment it once; exact replays do
   not increment it. If a mutation compares version N, its returned proposal
   carries N+1. Exact approval or rejection compares that proposal version and
   completes at N+2. Stale, invalid, and failed operations do not increment it.
3. Explicit `canon` and `revealed` transitions are allowed only when shown in
   the exact diff and explicitly approved. Approval alone never promotes a
   record.
4. The editor extends the existing proposal contract through authoritative
   proposal v2, preserving v1 authority and error meanings while supporting
   create, remove, and multi-connection changes.
5. Warden-safe visibility metadata is explicit. Visibility changes appear in
   the exact diff, widening requires approval, and no automatic widening is
   permitted.
6. Existing hyphenated Atlas/domain record IDs are accepted directly by
   proposal v2 and the editor contract. No legacy-ID mapping or translation
   table exists; one-character IDs are valid; path-like values remain invalid.
- Adapter metadata may define additional fields, optional sections, allowed
  status transitions, and the provisional default. The current Mothership
  template defaults remain the local default; future adapters must supply their
  own values.
- The exact visual diff algorithm is an implementation detail. The observable
  requirement is complete, readable before/after content and structured
  metadata, authority, connection, and removal changes.

## 21. Design risks and handoff

### Risks

- The editor proposal examples must keep the seven mutation-specific card
  shapes aligned as the implementation adds create, remove, connection, and
  affected-reference changes.
- v1 removal safety is intentionally limited to typed `## Connections`; any
  broader reference syntax requires a separately authorized contract change.
- Visibility and Warden-only values are not present in the current Atlas record
  view. The public record/editor read contract must expose enough redacted,
  Warden-safe metadata for the form without weakening player boundaries.
- The current browser reference renders raw source and a single-record proposal.
  It is useful for authority, revision, provider, and exact-diff language, but
  it is not an editor implementation.

### Handoff to parent and downstream implementers

- Design artifact: `docs/hosted-record-editor-ux.md`.
- Specified states and interactions: head versus historical mode, create/edit/
  remove, adapter fields and sections, structured connections, impact review,
  exact diff, validation, stale/conflict/correction, replay, provider outage,
  approval, rejection, and destructive confirmation.
- Accessibility and responsive expectations: semantic form and dialog behavior,
  keyboard operation, live announcements, error focus, color-independent diff
  meaning, wrapping, stacked 320 CSS pixel layout, and no horizontal page
  scroll.
- Implementation constraints: current-head writes only; stable IDs; explicit
  outgoing connections and derived backlinks; domain identifiers only; no raw
  path or database route; no direct mutation; exact validated approval; no
  silent merge, cascade, or canon promotion.
- Proposal v2 registration, typed connection impact coverage, and explicit
  Warden-safe visibility metadata are settled in Issue #212 and are reflected
  by the editor contract package.
- No API, schema, code, GitHub, Project field, or campaign-content files were
  changed by this design package.
