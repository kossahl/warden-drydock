# Hosted MVP Responsive Wireframes and Interaction Rules

Status: low-fidelity Phase 1 design. Boxes describe hierarchy and behavior, not
visual styling or implementation components.

## Shared desktop frame

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Warden Drydock · Localhost     Campaign: Hadley's Hope ▾    Provider: Ready │
├──────────────┬──────────────────────────────────────────────────────────────┤
│ Atlas        │ Rev viewed: 12 · Head: 12 · Authority: Canon · Synced       │
│ Prepare      ├──────────────────────────────────────────────────────────────┤
│ Live         │                                                              │
│ Proposals 3  │                    Current section                           │
│ Revisions    │                                                              │
│              │                                                              │
│ Settings     │                                                              │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

The authority strip is sticky below the app header. If the view is stale, it
reads, for example, `Viewing Rev 12 · Head 13 · Read-only older revision` and
offers `Go to head`. During play it adds `Live base: 12`. Offline/local capture
state takes priority over the normal `Synced` summary.

## Setup gate

### Desktop

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Warden Drydock · Localhost                                   Setup required │
├─────────────────────────────────────────────────────────────────────────────┤
│ Set up AI assistance                                                        │
│ The app sends only campaign excerpts chosen by deterministic retrieval.     │
│ [Review what leaves this computer]                                          │
│                                                                             │
│ 1 Provider                                                                 │
│   Provider [supported choice ▾]    Configuration [provider-defined fields…]│
│   [Verify connection]                 Status: Failed — authentication        │
│                                       [Edit configuration] [Retry]           │
│                                                                             │
│ 2 Data-transfer consent                                                     │
│   Provider: … · Handling notice: …                         [Review details] │
│   [ ] I consent to sending minimal retrieved campaign excerpts.             │
│                                                                             │
│ [Continue to campaigns — unavailable until verified and consented]          │
└─────────────────────────────────────────────────────────────────────────────┘
```

Verification status is announced without moving focus. On failure, focus moves
to the failure summary only when verification was explicitly submitted; the
summary links back to the invalid field or Retry. Credential values are never
rendered back into readable text.

### Narrow viewport

```text
┌──────────────────────────────┐
│ Warden Drydock     Setup     │
├──────────────────────────────┤
│ Set up AI assistance         │
│ What leaves this computer…   │
│ [Review details]             │
│                              │
│ Provider                     │
│ [supported choice ▾]         │
│ Provider configuration       │
│ [provider-defined fields…]   │
│ [Verify connection]          │
│ Failed — authentication      │
│ [Edit configuration] [Retry] │
│                              │
│ Data-transfer consent        │
│ Provider … · Notice …        │
│ [ ] I consent to minimal…    │
│                              │
│ [Continue — unavailable]     │
└──────────────────────────────┘
```

Content remains in workflow order; no two-column dependence.

## Campaign list and creation

### Campaign list desktop / narrow adaptation

```text
Desktop                                      Narrow
┌──────────────────────────────────────┐     ┌──────────────────────────────┐
│ Campaigns           [Create campaign]│     │ Campaigns                    │
│ Search [___________________________] │     │ [Create campaign]            │
│ ┌──────────────────────────────────┐ │     │ Search [__________________]  │
│ │ Hadley's Hope · Mothership       │ │     │ ┌──────────────────────────┐ │
│ │ Head Rev 12 · Synced · Open      │ │     │ │ Hadley's Hope            │ │
│ └──────────────────────────────────┘ │     │ │ Mothership · Head Rev 12│ │
└──────────────────────────────────────┘     │ │ Synced          [Open]   │ │
                                           │ └──────────────────────────┘ │
                                           └──────────────────────────────┘
```

Empty: `No campaigns yet. Create a Mothership campaign to begin.` plus only
`Create campaign`. Creation shows the exact progress label (`Creating` then
`Validating`) in the page title and campaign row. `Needs attention` includes
the failed stage, findings, and Retry; no `Open workspace` action appears before
`Ready`.

### Campaign creation desktop / narrow adaptation

```text
Desktop                                      Narrow
┌──────────────────────────────────────┐     ┌──────────────────────────────┐
│ Create a Mothership campaign         │     │ Create campaign              │
│ Campaign name                        │     │ Mothership pilot             │
│ [__________________________________] │     │ Campaign name                │
│ Required campaign facts…             │     │ [__________________________] │
│ [field] [__________________________] │     │ Required facts…              │
│                                      │     │ [__________________________] │
│ [Cancel]           [Create campaign] │     │ [Create campaign]            │
└──────────────────────────────────────┘     │ [Cancel]                     │
                                           └──────────────────────────────┘

Creating / Validating
┌─────────────────────────────────────────────────────────────────────────────┐
│ Creating campaign                                                           │
│ ✓ Input accepted  →  Creating  ·  Validating  ·  Ready                     │
│ Current stage: Creating with deterministic Drydock operations               │
│ Keep this page open while this stage completes.                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

Only facts required by the initializer appear. The progress action text must
match the supported cancellation/background behavior; it must not promise that
an in-flight deterministic mutation can be cancelled unless architecture
provides safe cancellation.

## Atlas

### Desktop

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Atlas · Library | Relationships | History          Source: Rev 12 · Canon   │
├───────────────────────┬─────────────────────────────────────────────────────┤
│ Search [/___________] │ Record: Station Erebos                 [Open sources]│
│ Type [All ▾]          │ Authority: Canon · Record version: Rev 10            │
│ Authority [All ▾]     │ Summary…                                              │
│ 37 records            ├─────────────────────────────────────────────────────┤
│                       │ Relationships   [Graph] [Relationship list]           │
│ > Station Erebos      │  ┌───────────┐    supplies    ┌───────────────┐      │
│   Dr. Vale            │  │ Erebos    │───────────────>│ Relay Seven   │      │
│   Relay Seven         │  └───────────┘                └───────────────┘      │
│                       │  Same data: 2 typed links · 1 backlink                │
├───────────────────────┴─────────────────────────────────────────────────────┤
│ History [Approved ✓] [ ] Preparation — non-canon [ ] Proposals — non-canon │
│ [Timeline] [History list]                                     Source: Rev 12│
└─────────────────────────────────────────────────────────────────────────────┘
```

The segmented Graph/List and Timeline/List controls expose the same data. The
graph is a bounded neighborhood, not a campaign-wide canvas. Selecting a node
updates record detail and announces the new selection. Zoom/pan is optional;
keyboard and list access are not.

### Narrow viewport

```text
┌──────────────────────────────┐
│ Atlas              Rev 12 ▾  │
│ [Library][Links][History]    │
├──────────────────────────────┤
│ Search [/_______________]    │
│ [Filters (2)] · 37 records   │
│ > Station Erebos             │
│   Dr. Vale                   │
│   Relay Seven                │
├──────────────────────────────┤
│ Station Erebos               │
│ Canon · record Rev 10        │
│ Summary…                     │
│ [Open sources]               │
│                              │
│ [Graph] [Relationship list]  │
│ Relationship list selected   │
│ → supplies · Relay Seven     │
│ ← operated by · Dr. Vale     │
├──────────────────────────────┤
│ Atlas Prepare Live Proposals │
└──────────────────────────────┘
```

Library and detail stack in one scroll context. Selecting a result moves focus
to the record heading only after an explicit activation, not while arrowing
through results. Filters open a labeled modal sheet and return focus to the
trigger on close.

## Prepare and grounded AI

### Desktop

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Prepare                                 Viewed Rev 12 · Preparation context  │
├────────────────────────────────┬────────────────────────────────────────────┤
│ Request                        │ Deterministic sources                       │
│ [____________________________] │ 3 selected at Rev 12                        │
│ [____________________________] │ [Station Erebos] [Dr. Vale] [Session 04]    │
│ [Ask] [Check] [Generate]       │ [Inspect excerpts]                          │
├────────────────────────────────┴────────────────────────────────────────────┤
│ Authority: Draft · Provider output                                           │
│ Provenance: Grounded at Rev 12 · 3 deterministic sources                    │
│ …                                                                           │
│ Sources: Station Erebos; Dr. Vale; Session 04                               │
│ [Create proposal] [Copy draft]                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

If evidence is missing or contradictory, a finding panel precedes any provider
submission. If the provider fails, the prompt and exact source set remain, with
`Retry same grounded request`; Atlas navigation remains available.

On narrow screens the order is Request → Source summary → Draft. `Inspect
excerpts` opens a sheet and returns focus to the source summary. The Draft label
stays in the response heading even when scrolled.

## Live cockpit

### Desktop

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ LIVE · Base Rev 12 · Head 13 · grounding: Rev 12 + confirmed table facts   │
│ [Ask] [Check] [Generate] [Capture]                         [End session]     │
├──────────────────────────────────────────────┬──────────────────────────────┤
│ Capture                                      │ Confirmed table facts        │
│ Type: [Confirmed table fact ▾]               │ 20:14 Airlock opened         │
│ [__________________________________________] │ Confirmed table fact · Synced│
│ [__________________________________________] │ 20:17 Vale entered alone     │
│ [Record]                                     │ Confirmed table fact         │
│                                              │ Saved on device              │
│                                              ├──────────────────────────────┤
│ Provider unavailable. Capture still works.   │ Unresolved questions         │
│                                              │ 20:19 What caused signal?    │
│                                              │ Unresolved question          │
│                                              │ Not grounding evidence       │
│                                              │ Needs attention              │
├──────────────────────────────────────────────┴──────────────────────────────┤
│ Saved on device means this browser has a durable copy; it is not yet synced.│
└─────────────────────────────────────────────────────────────────────────────┘
```

The current mode is a tab with text and selected-state semantics. Capture is
the default recovery focus after provider failure. `Record` returns success
only after device-local persistence, adds one item to its type-specific list,
and announces its `Confirmed table fact` or `Unresolved question` label plus
`Saved on device`. Later sync updates that same item to `Syncing` then `Synced`.
Only items labeled `Confirmed table fact` enter live grounding.

Ask, Check, and Generate results always begin with `Authority: Draft`. A
separate provenance line names grounded sources and revision; it never replaces
or qualifies the Draft authority label.

### Narrow viewport

```text
┌──────────────────────────────┐
│ LIVE · Base 12     Status ▾  │
│ Head 13; grounding unchanged │
├──────────────────────────────┤
│ [Ask][Check][Generate][Capture]│
│ Capture selected             │
│ [Confirmed table fact ▾]     │
│ [__________________________] │
│ [Record]                     │
│ Saved on device              │
│ Not yet synced               │
├──────────────────────────────┤
│ Confirmed table facts  [All] │
│ Airlock opened               │
│ Confirmed table fact         │
│ Synced                       │
│ Vale entered                 │
│ Confirmed table fact         │
│ Saved on device              │
│ Unresolved questions   [All] │
│ What caused signal?          │
│ Unresolved question          │
│ Not grounding evidence       │
├──────────────────────────────┤
│ [End session]                │
│ Atlas Prepare Live Proposals │
└──────────────────────────────┘
```

The capture editor and Record action remain above recent facts and reachable
without horizontal scrolling. The status summary expands to list base, head,
provider, service, local save, and sync states. End session remains labeled and
requires confirmation describing pending local captures; it is never adjacent
to Record as an icon-only control.

### Offline end state at either width

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Ended - review pending · Offline                                             │
│ End intent: Saved on device · Not synced                                     │
│ Unsynced queue: 2 confirmed facts · 1 unresolved question · 1 end intent     │
│ Proposals: Not created — waiting for successful synchronization              │
│ Canon/head revision: Unchanged                                               │
│ [Retry connection] [Inspect device queue]                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

On reconnect, the end intent and typed captures update in place through
`Syncing` to `Synced`; duplicate acknowledgement must not add a second end
event, fact, or question. Only confirmed table facts are eligible for live
grounding. Only after successful end-intent and confirmed-fact synchronization
can the service create the correctable proposals; unresolved questions remain
separately labeled review items. This state never claims server sync, proposal
creation, canon change, or a new revision.

## Proposal review and conflict

### Desktop

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Proposal P-104 · Needs review        Base Rev 12 · Current head Rev 12       │
│ Origin: Live session 05              Validation: Passed                      │
├─────────────────────────────────────────────────────────────────────────────┤
│ Change 1 of 2 · Station Erebos                                             │
│ - status: Preparation                                                      │
│ + status: Canon             ← Explicit authority promotion                  │
│ - airlock: sealed                                                          │
│ + airlock: opened                                                          │
│ [Previous] [Next]                                     [Edit proposal]       │
├─────────────────────────────────────────────────────────────────────────────┤
│ Approving applies only this displayed mutation and creates one revision.    │
│ [Reject] [Validate again] [Approve proposal]                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

Conflict replaces approval controls with:

```text
Conflict — proposal base Rev 12 no longer matches head Rev 13.
[Compare base, proposal, and head] [Correct proposal] [Reject]
No changes have been applied.
```

The comparison is readable as structured changes, not color-only red/green.
Add/remove/change prefixes, field labels, old/new values, and authority effects
are announced in the accessibility tree.

### Narrow viewport

Proposal metadata precedes each change. Previous/Next become `Change 1 of 2`
controls; the persistent action bar contains `Reject` and the currently allowed
primary action. Approve is absent/disabled with an adjacent reason during
validation failure, stale base, or conflict. The full consequence sentence
appears immediately before approval.

## Revisions

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Revisions                                                                   │
├─────────────────────────────┬───────────────────────────────────────────────┤
│ Rev 13 · HEAD · Approved    │ Rev 13 · Immutable · Validation passed       │
│ Rev 12 · Approved           │ Compared with Rev 12                         │
│ Rev 11 · Approved           │ 4 deterministic changes                     │
│                             │ [View in Atlas] [View read-only diff]         │
└─────────────────────────────┴───────────────────────────────────────────────┘
```

On narrow screens, selecting a revision opens its detail below the list or on a
new internal page with a clear Back action. `View in Atlas` announces that the
user is entering an older read-only revision when it is not head. There are no
import, export, download, Git, restore-head, or destructive history controls.

## Settings

### Desktop / narrow adaptation

```text
Desktop                                      Narrow
┌──────────────┬─────────────────────────┐   ┌──────────────────────────────┐
│ Settings     │ Provider & data transfer│   │ Settings                     │
│ > Provider   │ Status: Ready           │   │ [Provider & data transfer ▾] │
│   Campaign   │ Provider: …             │   │ Status: Ready                │
│   Accessibility │ Configuration: ready │   │ Provider: …                  │
│              │ Consent: current        │   │ Configuration: ready         │
│              │ [Review handling]       │   │ Consent: current             │
│              │ [Reverify] [Change]     │   │ [Review handling]            │
│              │ [Remove configuration]  │   │ [Reverify]                   │
└──────────────┴─────────────────────────┘   │ [Change provider]            │
                                           │ [Remove configuration]       │
                                           └──────────────────────────────┘
```

Changing provider or accepting a materially changed handling notice does not
reuse the old consent. A confirmation explains that the app will return to the
setup gate; campaign snapshots remain unchanged. Removing configuration is
consequential but not presented as deleting campaign content.

## Responsive behavior matrix

| Element | Desktop | Narrow viewport |
| --- | --- | --- |
| Primary navigation | Labeled left rail | Labeled persistent bottom navigation or menu; current section visible |
| Authority strip | Full sticky row | Condensed text summary plus always-visible critical state; expandable details |
| Atlas | Library/detail split with bounded graph | Stacked list/detail; list representation preferred by default when graph would be cramped |
| History | Timeline with adjacent filters and list toggle | History list default; timeline optional if legible |
| Prepare | Request/sources side by side; draft below | Request, sources, draft in document order |
| Live | Main action plus fact rail | Action first, compact recent facts, full feed in sheet/page |
| Proposal diff | Metadata, structured diff, persistent actions | One change at a time, structured old/new labels, persistent allowed action |
| Tables | Visible columns with responsive priority | Record cards or labeled rows; no meaning lost by hidden columns |

No control depends on hover. Touch targets must meet the implementation’s
adopted accessible target-size standard, and content reflows without two-axis
scrolling at 320 CSS pixels except an optional visualization canvas whose full
data remains available in the adjacent list mode.

## Keyboard model

Global behavior:

- `Tab` and `Shift+Tab` follow visual/document order; focus is always visible.
- `Enter` activates links and primary row actions; `Space` activates buttons,
  checkboxes, and toggles according to native semantics.
- `Escape` closes the topmost non-destructive dialog/sheet and restores focus
  to its trigger. It never discards typed content without warning.
- `/` focuses Atlas search when the current focus is not an editable field.
- `Ctrl+K` on Windows/Linux and `Command+K` on macOS opens a labeled command
  menu. All shortcuts are discoverable there and available as ordinary buttons.
- Tabs use arrow-key movement and the documented automatic/manual activation
  behavior consistently. Lists use normal browser focus or documented
  listbox/tree semantics; custom key behavior must not mimic semantics it does
  not fully implement.

Recommended fixed live shortcut sequences, active only outside editable fields:

- `g` then `f`: **Record table fact** and focus Capture.
- `g` then `r`: **Recall canon** and focus Ask.
- `g` then `q`: **Capture unresolved question**.
- `g` then `e`: **End session**, opening the confirmation rather than ending
  immediately.

The command menu displays these sequences and lets the Warden invoke the same
actions without memorization. This sequence vocabulary is a product-design
recommendation to validate in the pilot; the four shortcut capabilities are
required by the work package.

Graph keyboard behavior:

- Graph nodes are reachable in a documented, stable order from the selected
  record outward; arrow keys move between spatial neighbors when the graph has
  focus, `Enter` selects, and `Escape` returns focus to the Graph/List toggle.
- Each node’s accessible name includes record name and type. Each edge is
  available in the Relationship list with direction and relationship label.
- The `Relationship list` must be a complete alternative and can be set as the
  persistent preference; no graph-only command or fact is permitted.

Timeline keyboard behavior:

- Timeline entries use chronological document order and are reachable without
  horizontal drag. A keyboard user can switch to `History list` at the same
  filter and position.
- Overlay toggles are independent checkboxes named `Show preparation —
  non-canon` and `Show proposals — non-canon`. Approved history remains visibly
  active and cannot be mistaken for an overlay.
- Timeline date/group labels are headings or equivalent programmatic groups;
  entries expose date, title, authority, source record, and revision.

## Accessibility and feedback requirements

- Use native elements and landmarks: banner, navigation, main, complementary
  where appropriate, and one page-level heading. A skip link targets main
  content.
- Status is conveyed by text plus optional icon and color. Required vocabulary
  remains exact: `Draft`, `Saved on device`, `Syncing`, `Synced`, `Needs
  attention`, `Conflict`, and revision labels.
- Polite live-region announcements cover verification, retrieval completion,
  provider failure, device save, sync transitions, and background head changes.
  Validation failure and destructive/consequential confirmation use focused
  summaries, not repeated assertive announcements.
- Streaming AI text can be paused or presented in buffered chunks so screen
  readers are not flooded. Focus does not jump as tokens arrive.
- Loading placeholders have accessible names; previously known authority and
  revision state remain visible during refresh.
- Every form error is associated with its field and summarized with links after
  submit. Instructions do not rely on placeholder text.
- Structured diffs identify unchanged/removed/added/authority-changed content
  in text. Color is supplementary.
- Dialogs trap focus only while modal, have an accessible name, and return focus
  predictably. Toasts never contain the sole copy of a failure or sync state.
- Motion is non-essential and respects reduced-motion preference. Graph layout
  changes do not animate in a way that obscures focus.
- Text zoom and browser zoom preserve content and actions. Truncation includes a
  programmatic/full-text reveal.

## UX acceptance criteria

1. From a clean environment, a keyboard-only Warden can configure and verify a
   provider, review and affirm data-transfer consent, and reach Campaigns; the
   workspace is unreachable before all three conditions are current.
2. The Warden can create a Mothership campaign in the browser and sees
   `Creating`, `Validating`, and either `Ready` or actionable `Needs attention`;
   a workspace opens only for a validated initial revision.
3. Every workspace primary view exposes viewed/head revision and relevant
   authority. Live additionally exposes `base_revision`; local capture views
   expose device-save and sync state.
4. Atlas library counts, record details, relationship list, backlinks, and
   history list are usable at desktop and 320 CSS pixels without graph or
   timeline interaction. Their visual counterparts expose the same data.
5. Approved history is the timeline default. Preparation and Proposal overlays
   begin off, are independently toggled, and remain labeled non-canon.
6. Every provider output, including grounded Ask and Check answers, is labeled
   `Draft`. Sources and revision are exposed as provenance, never as authority.
   Missing or contradictory evidence is stated before generation rather than
   silently completed.
7. Provider failure preserves the request and retrieved source set. During an
   active session, Capture remains usable and clearly reports local persistence
   even when provider or local service calls fail.
8. Capture requires an explicit `Confirmed table fact` or `Unresolved question`
   type and does not report success before durable device-local persistence.
   The two types remain in separate labeled lists; only confirmed table facts
   enter live grounding. Reload restores the typed item, reconnection updates
   that same item through `Syncing` to `Synced`, and duplicate retry never
   produces a second visible item.
9. Ending offline durably records the end intent on device and shows `Ended -
   review pending` with an explicit unsynced queue. Reconnection synchronizes
   that intent and typed captures idempotently; no proposal, canon change,
   server sync, or new revision is claimed before success. Unresolved questions
   remain labeled workflow items and never enter grounding.
10. If head advances during live play, the UI names both revisions and continues
   grounding at `base_revision` plus confirmed table facts.
11. Proposal review shows an atomic structured diff, validation, base/head, and
    explicit authority transitions. Approval cannot occur on validation
    failure, stale revision, or conflict.
12. Approving a proposal does not imply canon promotion: every promotion is a
    separately visible change in the reviewed diff. Successful approval creates
    one revision; rejection and failed/conflicted apply create none.
13. A two-tab stale mutation preserves unsent local text, names the newer
    authoritative state, and requires explicit recovery; there is no silent
    last-write-wins result.
14. All state meanings and actions are conveyed without color, keyboard focus
    is visible and restored predictably, and graph/timeline facts have complete
    text alternatives.
15. No primary or empty-state surface exposes import, export, player, remote,
    VTT, audio, billing, multi-system, or autonomous-GM controls.

## Frontend and architecture implementation inputs

Frontend needs semantic, revision-stable view models for the states and fields
specified in the information architecture; explicit captured-item type and
grounding eligibility; stable identity for list focus and sync reconciliation;
an accessible status-announcement strategy; and identical data feeding each
graph/list and timeline/list pair.

Architecture needs to define provider/consent, verification, creation retry,
live-session concurrency, device/server idempotency, head-change notification,
proposal precondition, and atomic validation/apply contracts. Those contracts
must expose the observable states above without returning provider credential
values or treating workflow/audit/draft data as campaign content. This document
intentionally does not prescribe endpoints, payloads, database tables,
device-local persistence mechanisms, storage libraries, or
frontend/provider/hosting technology.
