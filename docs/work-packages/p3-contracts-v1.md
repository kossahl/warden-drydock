# P3-CONTRACTS v1: Hosted domain contracts

## Status

Ready for implementation.

## Phase and ownership

- **Phase:** 3 — Engine and contract foundation
- **Work-package ID:** `P3-CONTRACTS`
- **Version:** 1
- **Responsible role:** `hosted_backend_implementer`
- **Owned files:** `docs/contracts/hosted/**` and `tests/hosted/contracts/**`
- **Pinned baseline:** `0307671537ac3c30d9f202a5166d1043a403ead8`
- **Branch and PR target:** `codex/p3-contracts` to `master`; draft PR

The implementer is not authorized to change core, CLI, standalone, database,
provider, frontend, Compose, or campaign-template implementation. Contract
requirements that cannot be expressed inside the owned paths require a
`DECISION REQUIRED` handoff before expanding ownership.

## User problem and desired outcome

Backend, core, and frontend work must not independently invent identifiers,
authority states, errors, revision bindings, idempotency behavior, or recovery
semantics. Phase 3 establishes one versioned, executable contract vocabulary so
later agents can implement against reviewed schemas and fixtures without relying
on conversation history.

The desired outcome is a public-safe contract package that precisely represents
the accepted localhost single-Warden pilot. It preserves immutable standalone
campaign snapshots, deterministic retrieval, Draft-first AI output, explicit
Warden approval, and offline-safe live capture.

## Deliverables

Create a contract index and compatibility policy plus versioned JSON Schemas,
examples, and negative fixtures for these families:

1. API envelope, request identity, public identifiers, structured findings,
   safe errors, conflicts, and idempotent operation requests.
2. Campaign, revision, entity, connection, Atlas record, neighborhood graph,
   backlink, history, and comparison view models.
3. Engine command, staged result, and deterministic finding envelopes. Engine
   contracts accept opaque server-issued handles and have no publication field
   or authority.
4. Snapshot manifest, canonical tree/file hashes, lineage, head expectation,
   publication intent, quarantine, projection checkpoint, and rebuild result.
5. Retrieval citation and source envelope, including pinned campaign/revision,
   authority, deterministic ordering, excerpt counts, and source-set digest.
6. Provider readiness, redacted configuration, consent identity, normalized
   generation stream, usage, retryable failure, terminal Draft, and resume.
7. Session, controller epoch, observation/takeover, typed live event, device
   operation, acknowledgement, replay conflict, ordering, end barrier, and
   non-canon overlay.
8. Generated Draft, immutable Canon Proposal version, exact diff digest,
   validation, correction, rejection, conflict, and approval binding.
9. Audit event, idempotency receipt, reconciliation classification, backup
   manifest, liveness, readiness, and provider-degraded state.

Use JSON Schema Draft 2020-12 for data contracts. Keep schemas split by bounded
family and publish a single versioned index that names every schema and fixture.
OpenAPI may be added only where it improves HTTP interoperability without
duplicating or weakening the canonical JSON Schemas. No endpoint inventory is
required in this package.

## In scope

- Contract documentation, authority ownership, compatibility rules, redaction
  rules, deterministic canonicalization requirements, and error taxonomy.
- Machine-readable schemas for every contract family above.
- Synthetic positive examples and negative fixtures for material invariants.
- Tests that discover the contract index, load every schema and fixture, and
  prove positive validation and expected negative rejection.
- Stable identifiers and enums justified by the accepted ADRs and current
  Mothership workflows.

## Non-goals

- Executable API endpoints, database schemas or migrations, persistence, Docker,
  frontend code, provider calls, or engine implementation.
- Campaign ZIP import, export, Git/directory synchronization, or legacy campaign
  migration.
- Remote hosting, authentication, tenancy, player access, billing, or multiple
  RPG systems.
- Provider selection, paid calls, embeddings, or browser-storage selection.
- Generic shell, filesystem, SQL, Git, arbitrary HTTP, apply, approve, promote,
  or arbitrary-path capabilities.

## Authoritative decisions

- `docs/product-decisions.md`
- `docs/adr/001-ai-assisted-product-interface.md`
- `docs/adr/002-standalone-campaign-repositories.md`
- `docs/adr/003-adapter-boundary.md`
- `docs/adr/004-hosted-engine-api-boundary.md`
- `docs/adr/005-hosted-snapshot-workflow-storage.md`
- `docs/adr/006-local-compose-security-operations.md`
- `docs/architecture/hosted-mvp.md`
- `docs/design/hosted-mvp-information-architecture.md`
- `docs/design/hosted-mvp-flows.md`
- `docs/design/hosted-mvp-wireframes.md`

Repository documents and accepted ADRs override issue or PR comments. A comment
cannot silently change authority, scope, or state transitions.

## Required contract rules

- Every top-level payload declares its contract name and positive integer
  version; unknown versions fail closed.
- Public identifiers are opaque strings with bounded syntax. They are never
  interpreted as paths, URLs, database keys, or provider identifiers.
- Mutating requests bind an idempotency key, request-payload digest, expected
  revision or workflow version where applicable, and safe request identifier.
- Reusing an idempotency key with the same digest returns the prior result;
  reusing it with another digest is a conflict.
- A live session remains pinned to one `base_revision`. A newer campaign head
  may be reported but never silently changes grounding.
- Device replay is keyed by session, device, and operation. Exact replay is
  acknowledged; digest mismatch conflicts. Questions never become grounding
  facts.
- Retrieval sources have deterministic order and bind campaign, revision,
  authority, stable source identifiers, excerpts, counts, and a source digest
  before provider generation.
- Provider configuration is redacted; credentials are represented only by an
  opaque reference or revision fingerprint. Verification is distinct from
  consent.
- Provider streams normalize ordered versioned events with monotonically
  increasing sequence numbers. Disconnect never authorizes cancellation or
  mutation; terminal content remains Draft.
- A proposal version is immutable. Approval binds its exact version, diff
  digest, source/base revision, and expected campaign head. Stale heads conflict
  without automatic merge or rebase.
- Snapshot publication and head advancement require a persisted publication
  intent. Missing or ambiguous intent is quarantined; reconciliation never
  infers authority from timestamps, ordinals, or tree presence alone.
- Errors and audit records contain only safe identifiers, codes, stages, and
  bounded metadata. They exclude credentials, prompts, excerpts, generated
  content, campaign text, cookies, and private paths.
- Provider degradation may disable AI-dependent actions but must not represent
  typed Capture as unavailable.

## Error taxonomy minimum

The schemas and documentation must distinguish at least:

- invalid or unsupported contract version;
- invalid public identifier or unsafe binding;
- validation finding;
- idempotency digest conflict;
- stale revision, workflow version, or controller epoch;
- authorization/capability rejection without sensitive detail;
- provider unavailable, retryable failure, and terminal failure;
- stream sequence or source-digest conflict;
- snapshot integrity, lineage, publication-intent, and quarantine failure;
- proposal validation or approval conflict;
- service unavailable because readiness or maintenance gates are closed.

## Acceptance criteria

1. A machine-readable index enumerates every contract family, schema version,
   authority owner, positive example, and negative fixture.
2. Every schema has a stable identifier, title, version, bounded objects with
   unknown properties rejected unless explicitly justified, and documented
   redaction behavior.
3. Positive examples validate against their declared schema.
4. Every negative fixture declares the schema and expected failure category and
   fails validation for that reason.
5. Negative coverage includes unknown versions, traversal-like identifiers,
   missing revision/source bindings, stale versions or epochs, duplicate stream
   sequence, invalid authority transition, changed idempotency digest, secret or
   private-path leakage, provider authority widening, and ambiguous publication
   intent.
6. Contracts cannot express arbitrary paths, generic tools, direct publication,
   approval by a provider, automatic canon promotion, or silent re-grounding.
7. Shared vocabulary covers `Campaign`, `Revision`, `Entity`, `Connection`,
   `Session`, `LiveEvent`, `SessionOverlay`, `GeneratedDraft`, `CanonProposal`,
   `RetrievalCitation`, `AuditEvent`, and `OperationRequest`.
8. Contract tests run under the existing unittest discovery command without a
   separate hidden runner.
9. Existing CLI, generated standalone behavior, and all current tests remain
   unchanged and green.
10. Documentation explicitly identifies deferred endpoint design, executable
    storage/API implementation, import/export, provider selection, and device
    storage as non-authorized follow-up work.

## Required verification

```text
python -m unittest tests.hosted.contracts -v
python -m unittest discover -s tests -v
python -m warden_drydock --help
git diff --check origin/master...HEAD
```

The implementer must additionally report the discovered schema/example/negative
fixture counts and demonstrate that at least one representative fixture fails
for each required failure category. A passing suite does not constitute semantic
approval of product or architecture decisions.

## Risks and recovery

- **Contract overreach:** adding endpoint or implementation choices can lock in
  unreviewed architecture. Recovery is to remove the speculative contract before
  acceptance; no runtime migration exists at this stage.
- **Under-specified authority:** a permissive schema could allow downstream
  agents to invent mutation paths. Fail closed and request a decision rather
  than adding generic extension points.
- **Vocabulary drift:** frontend and backend aliases can split the model. The
  versioned index is authoritative and names canonical terms once.
- **Fixture theater:** schema-valid examples alone do not prove security or
  semantics. Negative fixtures and an independent semantic review are required.
- **Public-repository leakage:** use only synthetic identifiers and prose. Do
  not include campaign content, secrets, prompts, local paths, or private logs.

Rollback is deletion or reversion of this documentation/test-only package before
dependent implementation begins. It performs no data migration and writes no
campaign state.

## Open questions and defaults

No product decision is currently blocking implementation.

- JSON Schema Draft 2020-12 is the documented default.
- Contract version 1 is the only accepted version; future additive compatibility
  must be documented rather than inferred.
- OpenAPI endpoint design is deferred; schemas remain transport-neutral.
- Device persistence technology and provider choice remain explicitly deferred.
- If a schema choice changes authority, canon, privacy, recovery, public API
  behavior, or another owner's files, the agent must return `DECISION REQUIRED`
  with two or three real options before proceeding.

## Required handoff

Return status, achieved contract behavior, changed files, schema/API impact,
exact checks and outcomes, deviations, new risks or questions, fixture counts,
and the next responsible role. The parent reviews and sanitizes the handoff
before any GitHub publication or dependent delegation.
