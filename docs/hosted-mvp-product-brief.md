# Hosted MVP Product Brief

## User problem and outcome

The primary user is one Warden creating, preparing, running, and maintaining a
Mothership campaign on their own computer. The current standalone product is
portable and deterministic, but exposes repository-oriented tooling and does
not provide a purpose-built table-time recovery path.

The personal pilot succeeds when the Warden can start a localhost application,
create a campaign, understand all campaign information through the Campaign
Atlas, prepare and play with source-visible AI assistance, capture table facts
through interruption, review proposed changes, and create validated immutable
revisions without leaving the browser for ordinary campaign work.

## Product boundary

The pilot is a local Docker Compose application for one Warden. AI is a required
part of the useful product rather than an optional enhancement. Before campaign
workspace access, onboarding requires a configured provider, its provider-defined credential, and
explicit consent to send the minimal excerpts selected by deterministic
retrieval. Atlas, live capture, and proposal behavior are implemented as part of
that AI-enabled workflow, not as a separately optimized offline-only product.

The pilot creates new campaigns through deterministic Drydock operations. It
does not provide campaign import, export, Git synchronization, player access,
remote hosting, VTT integration, audio, billing, multi-system support, or an
autonomous game master. Existing campaign data may be transferred manually for
development and the personal pilot.

## Authority model

| Concept | Product meaning |
| --- | --- |
| Canon | Warden-approved campaign truth in an immutable snapshot. |
| Preparation | Warden-only material anticipating play; it cannot establish what occurred. |
| Table fact | A Warden-recorded event from play. It overrides preparation during the active session but requires review before durable canon. |
| AI draft | Provider-generated material with no authority. |
| Proposal | An atomic, reviewable deterministic change set against one `base_revision`. |
| Revision | An immutable Markdown/frontmatter campaign snapshot. |
| Head revision | The selected approved revision; it advances only after validation and approval. |

Approving a proposal authorizes its displayed mutation. It does not implicitly
promote every affected record to canon; canon promotion must be explicit in the
reviewed diff.

## Core workflows

### Onboarding and creation

Onboarding explains what campaign content can leave the computer, configures
the chosen provider-defined credential, records consent locally, and verifies
the provider before unlocking the workspace. Campaign creation invokes the deterministic
initializer and creates the first immutable revision only after validation.

### Campaign Atlas

The record library exposes every supported record with deterministic search and
filters. A focused graph shows bounded typed neighbors and backlinks from a
selected record and always has an equivalent list representation. The timeline
defaults to approved history; preparation and proposals appear only as clearly
distinguished overlays. Every view identifies its source revision.

### Preparation and AI assistance

Deterministic retrieval chooses sources before provider invocation. Factual
answers identify supporting records and revision. Missing or contradictory
evidence is reported to the Warden instead of being invented. Generated content
remains a draft until a reviewed proposal applies it.

### Live session

Starting play records the current head as `base_revision`. Retrieval remains
grounded in that revision plus confirmed table facts even if head advances
elsewhere. The live cockpit provides four actions: **Ask**, **Check**,
**Generate**, and **Capture**. Ending play creates correctable proposals; only
validated approvals create a new revision.

### Offline survival and conflict

A captured table fact is durably stored on the device before success is shown.
The UI distinguishes `Saved on device`, `Syncing`, `Synced`, and `Needs
attention`. Reload and temporary service loss do not lose a fact. Reconnection
is idempotent. If head no longer matches `base_revision`, approval enters
`Conflict`; there is no silent merge or overwrite.

## User-visible states

- AI content: `Draft`.
- Proposal: `Draft`, `Needs review`, `Conflict`, `Approved`, or `Rejected`.
- Live session: `Live at revision ...`, `Ended - review pending`, or
  `Closed at revision ...`.
- Campaign creation: `Creating`, `Validating`, `Ready`, or `Needs attention`.
- Provider onboarding: `Setup required`, `Verifying`, `Ready`, or `Failed`.

State and authority must never be communicated by color alone.

## Acceptance criteria

- A clean pilot environment can complete provider onboarding and campaign
  creation entirely through the browser.
- Workspace access remains gated until provider configuration, verification,
  and explicit data-transfer consent succeed.
- Atlas record counts, graph relationships, backlinks, and timeline entries
  match deterministic campaign indexes.
- Repeated retrieval for the same revision and query selects the same source set
  before generation.
- Factual AI answers expose supporting records and revision; the evaluation
  fixture contains no unsupported factual claim.
- AI output cannot mutate an authoritative snapshot without an explicit
  approved proposal.
- A captured table fact survives reload and simulated service interruption and
  synchronizes exactly once.
- Live answers continue to use `base_revision` plus captured table facts when
  campaign head changes elsewhere.
- A stale proposal cannot overwrite a newer head.
- Approval creates one validated immutable revision; rejection creates none.
- Provider secrets, consent records, workflow audit data, and unapproved drafts
  do not become campaign content.
- Existing standalone CLI campaigns and deterministic operations remain valid.

Pilot measurements establish task completion and time for onboarding, campaign
creation, Atlas lookup, preparation, live capture, proposal review, and revision
approval. Correctness expectations are zero lost or duplicated table facts and
zero unsupported factual claims in the evaluation fixture. Provider latency,
failure rate, privacy terms, and measured cost are recorded during the bake-off;
numeric targets are set only after baseline evidence exists.

## Design constraints

- Keep revision, authority, proposal, and save/sync state continuously visible.
- Make sources and revision provenance reachable from factual AI answers.
- Distinguish proposal approval from explicit canon promotion.
- Cover empty, loading, provider failure, validation failure, offline recovery,
  stale revision, and conflict states.
- Preserve text/list alternatives for graph and timeline information.
- Optimize the live cockpit for rapid Warden capture and recovery.
- Do not expose controls that imply player access or import/export support.

## Architecture constraints

- Preserve immutable snapshot authority and rebuildable PostgreSQL projections.
- Expose deterministic domain operations, never generic AI-facing shell or
  filesystem access.
- Enforce revision preconditions, atomic validation, audit, idempotent sync, and
  explicit conflict recovery.
- Keep provider secrets, consent, and workflow-only state outside snapshots.
- Compare providers on identical grounded tasks before selection.

## Deferred decisions and revisit conditions

The Product Designer may recommend, but not silently decide, the exact fixed
shortcut vocabulary, keyboard behavior, timeline overlay interaction, and
maximum comfortable proposal size. The Architect defines the supported provider
consent record, secret storage, and concurrency contract without weakening the
product rules above.

Import and export return only after the personal pilot demonstrates a concrete
need and defines package format, snapshot authority, migration, validation,
history, and recovery behavior. Remote hosting and player access require a
separate privacy, authentication, tenancy, and threat-model decision.
