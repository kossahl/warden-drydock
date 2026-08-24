export const CONTRACT_VERSION = 2 as const;
export type PublicId = string;
export type Digest = string;
export type RevisionAuthority = "preparation" | "canon" | "revealed";
export type Authority = RevisionAuthority | "table_fact";
export type DraftAuthority = "draft";
export type SaveSyncState = "Saved on device" | "Syncing" | "Synced" | "Needs attention";
export interface ContractEnvelope<Name extends string> { contract_name: Name; contract_version: typeof CONTRACT_VERSION; }
export interface RevisionRef { revision_id: PublicId; ordinal: number; tree_digest: Digest; validation_status?: "pending" | "passed" | "failed"; }
export interface RecordSummary { record_id: PublicId; record_type: PublicId; name: string; authority: RevisionAuthority; }
export interface CampaignRevisionView extends ContractEnvelope<"campaign_revision_view"> { campaign_id: PublicId; campaign_name: string; adapter_id: "mothership"; viewed_revision: RevisionRef; head_revision: PublicId; records: ReadonlyArray<RecordSummary>; }
export interface RecordView extends ContractEnvelope<"record_view"> { campaign_id: PublicId; revision_id: PublicId; record_id: PublicId; record_type: PublicId; name: string; authority: RevisionAuthority; content: string; }
export interface GenerationSource { source_id: PublicId; authority: RevisionAuthority; revision_id: PublicId; order: number; excerpt: string; excerpt_digest: Digest; }
export type GenerationAction = "ask" | "check" | "generate";
export type GenerationContext = { scope: "campaign" } | { scope: "record"; record_id: PublicId; content_digest: Digest };
export interface GenerationView extends ContractEnvelope<"generation_view"> { generation_id: PublicId; campaign_id: PublicId; source_revision: PublicId; action: GenerationAction; context: GenerationContext; session_id: PublicId | null; draft_authority: "draft"; status: "pending" | "complete" | "failed" | "cancelled"; sources: ReadonlyArray<GenerationSource>; source_set_digest: Digest; last_sequence: number; terminal_content: string | null; terminal_content_digest: Digest | null; }
export interface GenerationEvent extends ContractEnvelope<"generation_event"> { generation_id: PublicId; sequence: number; event_type: "start" | "delta" | "usage" | "completion" | "failure" | "cancel"; draft_fragment: string | null; retryable: boolean | null; }
export interface ProposalChange { change_id: PublicId; subject_id: PublicId; change_type: "update"; record_type: PublicId; from_authority: RevisionAuthority; to_authority: RevisionAuthority; before_content: string; after_content: string; before_digest: Digest; after_digest: Digest; }
export interface ProposalView extends ContractEnvelope<"proposal_view"> { proposal_id: PublicId; proposal_version: number; campaign_id: PublicId; generation_id: PublicId; source_revision: PublicId; base_revision: PublicId; source_set_digest: Digest; terminal_draft_digest: Digest; artifact_kind: "proposal"; status: "draft" | "rejected" | "conflict" | "published" | "quarantined"; exact_diff: readonly [ProposalChange]; diff_digest: Digest; proposal_payload_digest: Digest; validation_status: "pending" | "passed" | "failed"; published_revision_id: PublicId | null; }
export type PublicErrorCategory = "unsupported_contract_version" | "unsafe_binding" | "validation_finding" | "idempotency_digest_conflict" | "stale_revision" | "capability_rejected" | "provider_unavailable" | "provider_retryable_failure" | "provider_terminal_failure" | "stream_sequence_conflict" | "source_digest_conflict" | "snapshot_integrity_failure" | "snapshot_lineage_failure" | "publication_intent_failure" | "quarantine_failure" | "proposal_validation_failure" | "proposal_approval_conflict" | "service_unavailable" | "not_found";
export interface PublicError { category: PublicErrorCategory; code: string; stage: string; request_id: PublicId; retryable: boolean; }
export type ProposalApprovalResult = ContractEnvelope<"proposal_approval_result"> & { proposal: ProposalView; exact_replay: boolean } & ({ outcome: "published"; published_revision: RevisionRef; error: null } | { outcome: "conflict"; published_revision: null; error: PublicError });
export interface ProviderReadiness extends ContractEnvelope<"provider_readiness_response"> { provider_configured: boolean; provider_available: boolean; consent_current: boolean; consent_identity_digest: Digest | null; ai_available: boolean; }
export type OperationName = "campaign_create" | "session_start" | "session_capture" | "session_end" | "proposal_correct" | "proposal_reject" | "proposal_approve" | "provider_configure" | "provider_consent" | "projection_rebuild" | "backup_create";
export interface OperationRequest extends ContractEnvelope<"operation_request"> { request_id: PublicId; operation: OperationName; idempotency_key: PublicId; payload_digest: Digest; expected_revision: PublicId | null; expected_workflow_version: number | null; subject_id?: PublicId; intent_digest?: Digest; }
export interface SourceRef { source_id: PublicId; revision_id: PublicId; authority: Authority; label: string; }
export interface DraftProvenance { authority: DraftAuthority; revision: RevisionRef; sources: ReadonlyArray<SourceRef>; }
