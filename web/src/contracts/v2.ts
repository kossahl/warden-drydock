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

export type AtlasAuthority = "preparation" | "canon" | "revealed";
export type KnownStatus = "idea" | "draft" | "review" | "canon" | "revealed" | "archived" | "accepted";
export type AtlasStatusFilter = KnownStatus | "missing" | "unknown";
export type RawStatus =
  | { classification: "known"; value: KnownStatus }
  | { classification: "missing"; value: null }
  | { classification: "unknown"; value: string };
export interface AtlasRevisionRef { revision_id: PublicId; ordinal: number; tree_digest: Digest; }
export interface AtlasBinding { campaign_id: PublicId; viewed_revision: AtlasRevisionRef; head_revision: AtlasRevisionRef; }
export interface AtlasRecordSummary { record_id: string; record_type: string; name: string; raw_status: RawStatus; authority: AtlasAuthority; summary: string; content_digest: Digest; }
export interface AtlasRecordFull extends AtlasRecordSummary { content: string; }
export interface AtlasFacet { value: string; count: number; }
export interface AtlasFacets { record_types: ReadonlyArray<AtlasFacet>; authorities: ReadonlyArray<AtlasFacet>; statuses: ReadonlyArray<AtlasFacet>; }
export interface AtlasCampaignItem { campaign_id: PublicId; campaign_name: string; adapter_id: "mothership"; recovery_state: "ready" | "rebuild_required" | "integrity_blocked"; head_revision: AtlasRevisionRef; projected_revision: AtlasRevisionRef | null; }
export interface AtlasCampaignCollection extends ContractEnvelope<"atlas_campaign_collection"> { campaigns: ReadonlyArray<AtlasCampaignItem>; }
export interface AtlasOverview extends ContractEnvelope<"atlas_overview"> { binding: AtlasBinding; campaign_name: string; adapter_id: "mothership"; record_count: number; edge_occurrence_count: number; approved_revision_count: number; facets: AtlasFacets; }
export interface AtlasFilters { record_types: ReadonlyArray<string>; authorities: ReadonlyArray<AtlasAuthority>; statuses: ReadonlyArray<AtlasStatusFilter>; }
export interface AtlasRecordLibraryResult extends ContractEnvelope<"atlas_record_library_result"> { binding: AtlasBinding; normalized_query: string; filters: AtlasFilters; limit: number; sort: "record_id"; total: number; items: ReadonlyArray<AtlasRecordSummary>; facets: AtlasFacets; next_cursor: string | null; previous_cursor: string | null; }
export interface AtlasRecordDetail extends ContractEnvelope<"atlas_record_detail"> { binding: AtlasBinding; record: AtlasRecordFull; }
export interface AtlasEdge { edge_id: string; occurrence_order: number; source_record_id: string; target_record_id: string; relationship: string; state: string; context: string; }
export interface AtlasNeighborhood extends ContractEnvelope<"atlas_depth_1_neighborhood"> { binding: AtlasBinding; depth: 1; limit: number; sort: "source_occurrence_edge"; focus: AtlasRecordSummary; neighbors: ReadonlyArray<AtlasRecordSummary>; edges: ReadonlyArray<AtlasEdge>; total_edges: number; next_cursor: string | null; previous_cursor: string | null; }
export type AtlasHistoryChangeKind = "added" | "removed" | "content_changed" | "metadata_changed" | "authority_transition";
export interface AtlasHistoryChange { record_id: string; change_kind: AtlasHistoryChangeKind; link_revision_id: PublicId; before_content_digest: Digest | null; after_content_digest: Digest | null; before_status: RawStatus | null; after_status: RawStatus | null; from_authority: AtlasAuthority | null; to_authority: AtlasAuthority | null; }
export interface AtlasHistoryEntry { revision: AtlasRevisionRef; parent_revision_id: PublicId | null; change_digest: Digest; affected_records: ReadonlyArray<{ record_id: string; link_revision_id: PublicId }>; changes: ReadonlyArray<AtlasHistoryChange>; proposal_id: PublicId | null; proposal_version: number | null; }
export interface AtlasHistoryCollection extends ContractEnvelope<"atlas_approved_history_collection"> { binding: AtlasBinding; subject_record_id: string | null; limit: number; sort: "revision_ordinal"; direction: "forward" | "backward"; total: number; entries: ReadonlyArray<AtlasHistoryEntry>; next_cursor: string | null; previous_cursor: string | null; }
export interface AtlasWorkflowSummary extends ContractEnvelope<"atlas_workflow_summary"> { binding: AtlasBinding; draft_generation_count: number; proposal_counts: { draft: number; rejected: number; conflict: number; published: number; quarantined: number }; active_session: null | { session_id: PublicId; base_revision: AtlasRevisionRef; workflow_version: number; confirmed_table_fact_count: number; unresolved_question_count: number }; }
export type AtlasGenerationStatus = GenerationView["status"];
export interface AtlasGenerationFilters { actions: ReadonlyArray<GenerationAction>; statuses: ReadonlyArray<AtlasGenerationStatus>; record_id: PublicId | null; }
export interface AtlasGenerationSummary { generation_id: PublicId; action: GenerationAction; context: GenerationContext; source_revision: AtlasRevisionRef; source_set_digest: Digest; status: AtlasGenerationStatus; retryable: boolean | null; created_at: string; }
export interface AtlasGenerationCollection extends ContractEnvelope<"atlas_generation_collection"> { binding: AtlasBinding; filters: AtlasGenerationFilters; limit: number; sort: "created_at_desc_generation_id_desc"; items: ReadonlyArray<AtlasGenerationSummary>; next_cursor: string | null; }
export interface AtlasProposalFilters { statuses: ReadonlyArray<ProposalView["status"]>; record_id: PublicId | null; }
export interface AtlasProposalSummary { proposal_id: PublicId; proposal_version: number; generation_id: PublicId; action: GenerationAction; context: GenerationContext; subject_record_id: PublicId; subject_content_digest: Digest; source_revision: AtlasRevisionRef; base_revision: AtlasRevisionRef; status: ProposalView["status"]; validation_status: "passed"; published_revision_id: PublicId | null; created_at: string; }
export interface AtlasProposalCollection extends ContractEnvelope<"atlas_proposal_collection"> { binding: AtlasBinding; filters: AtlasProposalFilters; limit: number; sort: "created_at_desc_proposal_id_desc_proposal_version_desc"; items: ReadonlyArray<AtlasProposalSummary>; next_cursor: string | null; }
