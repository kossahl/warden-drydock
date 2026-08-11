# Authority, redaction, and error policy

## Authority owners

- The application service owns request authorization, workflow transitions,
  idempotency, consent, proposal approval orchestration, publication intents,
  head comparison, audit, and recovery classification.
- The deterministic engine owns validation and staged candidate results. It
  receives only server-issued opaque handles and has no publication field.
- The snapshot service stores immutable trees but never infers or advances a
  campaign head.
- The provider emits normalized generation events and Draft content only. It
  cannot widen retrieval, approve, publish, promote, or choose another revision.
- The Warden's explicit action is required for consent, takeover, rejection,
  correction, and approval of an exact proposal version and diff.

`preparation`, `table_fact`, `canon`, and `revealed` are source authority
labels. A table fact can override preparation only inside its pinned live
session. A proposal must expose any promotion to `canon` or `revealed` as a
visible structured authority transition. Approval does not imply promotion.

## Redaction

Public contracts may contain opaque public identifiers, bounded stages and
codes, boolean secret presence, credential revision fingerprints, digests,
counts, and synthetic display metadata. They must not contain credentials,
cookies, CSRF values, prompts, source excerpts in errors or audit, generated
content in errors or audit, campaign text in errors or audit, provider-native events,
database identifiers, absolute/private paths, or local filesystem
handles.

The schemas intentionally distinguish content-bearing retrieval and Draft
contracts from safe diagnostic contracts. Implementations must apply the same
redaction recursively to bounded metadata; schema validity alone is not a log
authorization.

## Safe error taxonomy

Stable categories are: `unsupported_contract_version`, `unsafe_binding`,
`validation_finding`, `idempotency_digest_conflict`, `stale_revision`,
`stale_workflow_version`, `stale_controller_epoch`, `capability_rejected`,
`provider_unavailable`, `provider_retryable_failure`,
`provider_terminal_failure`, `stream_sequence_conflict`,
`source_digest_conflict`, `snapshot_integrity_failure`,
`snapshot_lineage_failure`, `publication_intent_failure`,
`quarantine_failure`, `proposal_validation_failure`,
`proposal_approval_conflict`, and `service_unavailable`.

Messages are optional bounded public prose and cannot carry source content.
Detailed private diagnostics stay behind the application boundary.
