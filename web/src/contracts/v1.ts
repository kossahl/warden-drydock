export const CONTRACT_VERSION = 1 as const;
export type PublicId = string;
export type Digest = string;
export interface ContractEnvelope<Name extends string> { contract_name: Name; contract_version: typeof CONTRACT_VERSION; }
export interface RevisionRef { revision_id: PublicId; ordinal: number; tree_digest: Digest; validation_status?: "pending" | "passed" | "failed"; }
export type AtlasAuthority = "preparation" | "canon" | "revealed";
export type KnownStatus = "idea" | "draft" | "review" | "canon" | "revealed" | "archived" | "accepted";
export type AtlasStatusFilter = KnownStatus | "missing" | "unknown";
export type RawStatus =
  | { classification: "known"; value: KnownStatus }
  | { classification: "missing"; value: null }
  | { classification: "unknown"; value: string };

export interface AtlasRevisionRef { revision_id: PublicId; ordinal: number; tree_digest: Digest; }
export interface AtlasBinding { campaign_id: PublicId; viewed_revision: AtlasRevisionRef; head_revision: AtlasRevisionRef; }
export interface AtlasRecordSummary {
  record_id: string;
  record_type: string;
  name: string;
  raw_status: RawStatus;
  authority: AtlasAuthority;
  summary: string;
  content_digest: Digest;
}
export interface AtlasRecordFull extends AtlasRecordSummary { content: string; }
export interface AtlasFacet { value: string; count: number; }
export interface AtlasFacets {
  record_types: ReadonlyArray<AtlasFacet>;
  authorities: ReadonlyArray<AtlasFacet>;
  statuses: ReadonlyArray<AtlasFacet>;
}
export interface AtlasCampaignItem {
  campaign_id: PublicId;
  campaign_name: string;
  adapter_id: "mothership";
  recovery_state: "ready" | "rebuild_required" | "integrity_blocked";
  head_revision: AtlasRevisionRef;
  projected_revision: AtlasRevisionRef | null;
}
export interface AtlasCampaignCollection extends ContractEnvelope<"atlas_campaign_collection"> { campaigns: ReadonlyArray<AtlasCampaignItem>; }
export interface AtlasOverview extends ContractEnvelope<"atlas_overview"> {
  binding: AtlasBinding;
  campaign_name: string;
  adapter_id: "mothership";
  record_count: number;
  edge_occurrence_count: number;
  approved_revision_count: number;
  facets: AtlasFacets;
}
export interface AtlasFilters {
  record_types: ReadonlyArray<string>;
  authorities: ReadonlyArray<AtlasAuthority>;
  statuses: ReadonlyArray<AtlasStatusFilter>;
}
export interface AtlasRecordLibraryResult extends ContractEnvelope<"atlas_record_library_result"> {
  binding: AtlasBinding;
  normalized_query: string;
  filters: AtlasFilters;
  limit: number;
  sort: "record_id";
  total: number;
  items: ReadonlyArray<AtlasRecordSummary>;
  facets: AtlasFacets;
  next_cursor: string | null;
  previous_cursor: string | null;
}
export interface AtlasRecordDetail extends ContractEnvelope<"atlas_record_detail"> { binding: AtlasBinding; record: AtlasRecordFull; }
export interface AtlasEdge {
  edge_id: string;
  occurrence_order: number;
  source_record_id: string;
  target_record_id: string;
  relationship: string;
  state: string;
  context: string;
}
export interface AtlasNeighborhood extends ContractEnvelope<"atlas_depth_1_neighborhood"> {
  binding: AtlasBinding;
  depth: 1;
  limit: number;
  sort: "source_occurrence_edge";
  focus: AtlasRecordSummary;
  neighbors: ReadonlyArray<AtlasRecordSummary>;
  edges: ReadonlyArray<AtlasEdge>;
  total_edges: number;
  next_cursor: string | null;
  previous_cursor: string | null;
}
export type AtlasHistoryChangeKind = "added" | "removed" | "content_changed" | "metadata_changed" | "authority_transition";
export interface AtlasHistoryChange {
  record_id: string;
  change_kind: AtlasHistoryChangeKind;
  link_revision_id: PublicId;
  before_content_digest: Digest | null;
  after_content_digest: Digest | null;
  before_status: RawStatus | null;
  after_status: RawStatus | null;
  from_authority: AtlasAuthority | null;
  to_authority: AtlasAuthority | null;
}
export interface AtlasHistoryEntry {
  revision: AtlasRevisionRef;
  parent_revision_id: PublicId | null;
  change_digest: Digest;
  affected_records: ReadonlyArray<{ record_id: string; link_revision_id: PublicId }>;
  changes: ReadonlyArray<AtlasHistoryChange>;
  proposal_id: PublicId | null;
  proposal_version: number | null;
}
export interface AtlasHistoryCollection extends ContractEnvelope<"atlas_approved_history_collection"> {
  binding: AtlasBinding;
  subject_record_id: string | null;
  limit: number;
  sort: "revision_ordinal";
  direction: "forward" | "backward";
  total: number;
  entries: ReadonlyArray<AtlasHistoryEntry>;
  next_cursor: string | null;
  previous_cursor: string | null;
}
export interface AtlasWorkflowSummary extends ContractEnvelope<"atlas_workflow_summary"> {
  binding: AtlasBinding;
  draft_generation_count: number;
  proposal_counts: { draft: number; rejected: number; conflict: number; published: number; quarantined: number };
  active_session: null | {
    session_id: PublicId;
    base_revision: AtlasRevisionRef;
    workflow_version: number;
    confirmed_table_fact_count: number;
    unresolved_question_count: number;
  };
}
