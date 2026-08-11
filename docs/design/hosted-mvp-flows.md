# Hosted MVP End-to-End Flows

Status: Phase 1 interaction specification. These flows define observable user
behavior and recovery, not API routes or persistence schemas.

## Flow conventions

- A boxed `Draft`, `Preparation`, `Confirmed table fact`, `Unresolved question`,
  `Proposal`, or `Canon` label is present whenever type or authority could be
  mistaken. An unresolved question is never grounding evidence.
- Revision references use a readable short label with a copy/reveal affordance
  for the full immutable identity.
- Error branches preserve entered work whenever doing so cannot imply a
  successful authoritative mutation.
- Provider-dependent generation and deterministic Drydock mutation are shown as
  separate stages.

## 1. Provider onboarding and consent

```mermaid
flowchart TD
    A["Open application"] --> B{"Configuration, verification, and consent current?"}
    B -- "Yes" --> H["Show campaigns"]
    B -- "No" --> C["Explain minimal excerpt transfer"]
    C --> D["Choose provider and complete provider-defined configuration"]
    D --> E["Verify provider"]
    E --> F{"Verification result"}
    F -- "Failed" --> G["Show safe reason; retry or edit setup"]
    G --> D
    F -- "Ready" --> I["Review provider-specific data-transfer consent"]
    I --> J{"Consent affirmed?"}
    J -- "No" --> K["Remain at setup gate"]
    K --> I
    J -- "Yes" --> H
    H --> L{"Provider or material handling later changes?"}
    L -- "Yes" --> M["Invalidate consent and return to setup gate"]
    M --> C
```

Behavior and recovery:

- Provider verification does not pre-check consent. Consent names the current
  provider/material-handling version and can be reviewed before affirmation.
- A failed verification never reveals credential values and does not unlock
  campaigns.
- If the service is unreachable during setup, the Warden sees that verification
  has not completed and can retry after service recovery.
- Removing configuration immediately gates workspace access. Existing campaign
  content remains untouched but is not exposed through an AI-enabled workspace
  without a ready provider and current consent.

## 2. Campaign creation

```mermaid
flowchart TD
    A["Campaigns"] --> B["Create campaign"]
    B --> C["Enter required campaign facts"]
    C --> D{"Client-side input valid?"}
    D -- "No" --> E["Show field errors and focus first error"]
    E --> C
    D -- "Yes" --> F["Creating: deterministic initializer"]
    F --> G{"Initializer succeeded?"}
    G -- "No" --> H["Needs attention: show safe stage and retry guidance"]
    H --> C
    G -- "Yes" --> I["Validating generated campaign"]
    I --> J{"Validation succeeded?"}
    J -- "No" --> K["Needs attention: show findings; no ready campaign"]
    K --> L["Retry safely or return to campaigns"]
    J -- "Yes" --> M["Create initial immutable revision"]
    M --> N["Ready: open Atlas at head revision"]
```

Recovery requirements:

- Retrying an interrupted request must resolve to one campaign or a clearly
  identified existing result, never a duplicate campaign created by ambiguity.
- A validation failure creates no usable head revision. Findings identify the
  affected input or deterministic stage without exposing repository internals
  as required user knowledge.
- Navigation away during creation requires an explicit safe consequence:
  cancel before mutation, or continue in background with a visible campaign-row
  state. Architecture decides which capability is supported; the frontend must
  not imply cancelability if the operation cannot be cancelled safely.

## 3. Atlas lookup, grounded retrieval, and generation

```mermaid
flowchart TD
    A["Open Atlas at selected revision"] --> B["Search, filter, or select record"]
    B --> C["Show library detail, relationships, backlinks, and history"]
    C --> D{"Need AI assistance?"}
    D -- "No" --> B
    D -- "Yes" --> E["Enter question or generation request"]
    E --> F["Run deterministic retrieval"]
    F --> G{"Evidence result"}
    G -- "Missing or contradictory" --> H["Show finding and selected sources; ask Warden how to proceed"]
    G -- "Grounded" --> I["Show selected sources and revision"]
    H --> J{"Send grounded request anyway?"}
    J -- "No" --> B
    J -- "Yes" --> K["Invoke provider with minimal retrieved context"]
    I --> K
    K --> L{"Provider result"}
    L -- "Failed" --> M["Preserve prompt and source set; retry or return to Atlas"]
    M --> K
    L -- "Succeeded" --> N["Show labeled Draft with sources and revision"]
    N --> O{"Create change?"}
    O -- "No" --> B
    O -- "Yes" --> P["Create small atomic proposal draft"]
    P --> Q["Open proposal review; no campaign mutation yet"]
```

Atlas equivalents:

- Moving focus through graph nodes and activating a node selects the same record
  as the relationship list. The list exposes all visible edge types,
  directions, and backlinks in reading order.
- Timeline and history list share filters and entries. Approved history is
  loaded by default; Preparation and Proposal overlays require separate opt-in
  toggles and remain labeled non-canon.
- Loading or provider failure never removes the selected revision or existing
  Atlas data. Repeating the same retrieval at the same revision exposes the same
  source set before provider generation.

## 4. Live start, use, capture, recovery, and end

```mermaid
flowchart TD
    A["Start live session"] --> B["Pin current head as base_revision"]
    B --> C["Live cockpit: Ask, Check, Generate, Capture"]
    C --> D{"Warden action"}
    D -- "Ask, Check, or Generate" --> E["Retrieve from base_revision plus confirmed table facts"]
    E --> F{"Provider available?"}
    F -- "No" --> G["Preserve request; show provider failure; Capture remains active"]
    G --> C
    F -- "Yes" --> H["Show Draft output with grounded sources and revision provenance"]
    H --> C
    D -- "Capture" --> I{"Choose captured item type"}
    I -- "Confirmed table fact" --> I1["Label Confirmed table fact; eligible for live grounding"]
    I -- "Unresolved question" --> I2["Label unresolved question; exclude from live grounding"]
    I1 --> J["Persist typed item to device before success"]
    I2 --> J
    J --> K["Show Saved on device"]
    K --> L{"Local service reachable?"}
    L -- "No" --> M["Queue safely; keep Capture available"]
    M --> N["Reconnecting"]
    N --> L
    L -- "Yes" --> O["Sync with idempotent retry identity"]
    O --> P{"Sync result"}
    P -- "Accepted or duplicate retry" --> Q["Show Synced once"]
    P -- "Needs attention" --> R["Keep local copy; inspect, retry, or correct"]
    R --> O
    Q --> C
    D -- "End session" --> S{"Unsynced captures or service loss?"}
    S -- "Yes" --> T{"Wait for sync or end locally?"}
    T -- "Wait or return to play" --> C
    T -- "End locally" --> V["Persist end intent; Ended - review pending with device queue"]
    V --> Y["Reconnect and synchronize end intent and typed captures idempotently"]
    Y --> Z{"End synchronization result"}
    Z -- "Failed or needs attention" --> AA["Keep device queue; show reason; retry"]
    AA --> Y
    Z -- "Accepted or duplicate retry" --> U["Create correctable proposal drafts"]
    S -- "No" --> U
    U --> W["Review proposals"]
    W --> X["Closed at approved revision when review is resolved"]
```

Live grounding and concurrency:

- If head advances in another tab, the cockpit announces the newer head but
  continues using the pinned `base_revision` plus confirmed table facts.
- The capture feed separates confirmed table facts from unresolved questions
  and identifies each item’s type plus whether it is only on device or also
  synced. Only confirmed table facts join live grounding. Unresolved questions
  and failed/invalid captures cannot silently join grounded retrieval.
- A duplicate sync response resolves the existing capture item by its stable
  identity; it never adds a second feed or timeline entry.
- Reload restores locally persisted captures and active-session context when
  supported. If the server says another tab owns a conflicting workflow state,
  preserve local unsent text, present the authoritative state, and require an
  explicit continue/refresh decision.
- Ending with a durable local queue must not claim the session is fully synced
  or that proposals already exist. The `Ended - review pending` view labels the
  end intent and each typed capture `Saved on device`, keeps the explicit
  unsynced queue and recovery available, and moves those same items through
  `Syncing` to `Synced` on reconnect. Proposal creation follows successful
  idempotent synchronization of the end intent and relevant confirmed facts;
  unresolved questions remain separately labeled review items.

## 5. Proposal correction, validation, approval, and conflict

```mermaid
flowchart TD
    A["Open atomic proposal"] --> B["Inspect origin, base revision, diff, and authority transitions"]
    B --> C{"Correction needed?"}
    C -- "Yes" --> D["Correct proposal draft"]
    D --> E["Run deterministic validation"]
    C -- "No" --> E
    E --> F{"Validation result"}
    F -- "Failed" --> G["Show findings; approval disabled"]
    G --> D
    F -- "Passed" --> H{"Base revision still satisfies head precondition?"}
    H -- "No" --> I["Set Conflict; compare base, proposal, and current head"]
    I --> J{"Warden decision"}
    J -- "Correct or deterministic rebase" --> D
    J -- "Reject" --> K["Rejected; create no revision"]
    H -- "Yes" --> L{"Approve displayed mutation?"}
    L -- "No" --> K
    L -- "Yes" --> M["Confirm any explicit canon promotions in diff"]
    M --> N["Apply deterministic mutation and validate atomically"]
    N --> O{"Apply result"}
    O -- "Conflict or validation failure" --> P["No new revision; return to Conflict or findings"]
    P --> D
    O -- "Succeeded" --> Q["Create one immutable revision and advance head"]
    Q --> R["Approved; show new revision and authority outcomes"]
```

Important semantics:

- Proposal approval authorizes only the displayed mutation. A change to canon
  is confirmed as an explicit authority transition in that diff.
- Closing a dialog, losing provider availability, or refreshing cannot approve
  a proposal.
- If head changes between final review and apply, the operation fails closed
  into `Conflict`; there is no silent merge, overwrite, or partial revision.
- Rejecting a proposal creates no revision. Corrections remain a proposal until
  validated and approved.

## 6. Two-tab and stale-read recovery

```mermaid
sequenceDiagram
    participant A as Tab A
    participant S as Authoritative service
    participant B as Tab B
    A->>S: Read head revision 12
    B->>S: Read head revision 12
    A->>S: Approve valid proposal against revision 12
    S-->>A: New head revision 13
    S-->>B: Head changed notification or stale response
    B->>S: Attempt approval against revision 12
    S-->>B: Conflict; current head revision 13
    Note right of B: Preserve unsent local text and show comparison
    B->>S: Submit corrected proposal against supported current base
```

For a read-only stale Atlas view, the Warden may continue reading revision 12 or
go to head. For mutation, stale context must be named and blocked until the
Warden chooses a supported correction path.

## Flow-level data and capability needs

| Flow | User-visible capability/data required | Architecture question retained |
| --- | --- | --- |
| Setup | Provider-defined configuration requirements, configuration-present state, verification outcome, consent requirement/currentness, material-change reason | Credential/configuration storage and consent record contract |
| Creation | Deterministic stage, validation findings, retry result, initial revision | Cancellation and idempotent creation mechanics |
| Atlas | Revision-scoped indexes, typed edges/backlinks, approved history and labeled overlays | Projection/query implementation |
| Retrieval | Stable pre-generation sources, evidence findings, revision provenance, provider progress/error | Provider orchestration and transport |
| Live | Stable session and base revision, ordered typed captures, grounding eligibility limited to confirmed facts, head-change signal | Active-session concurrency contract |
| Offline capture and end | Durable device-local typed captures and end intent, separate sync state, retry identity, duplicate acceptance result | Device/server persistence and reconciliation contract |
| Proposal | Atomic diff, authority transitions, validation, base/head comparison, outcome | Mutation/concurrency transaction contract |
| Revisions | Immutable identity, head marker, validated approval provenance, read-only comparison | Snapshot persistence and projection rebuild |

No row specifies an endpoint, payload, database table, or provider SDK.
