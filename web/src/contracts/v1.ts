export const CONTRACT_VERSION = 1 as const;

export type PublicId = string;
export type Digest = string;
export type Authority = "preparation" | "table_fact" | "canon" | "revealed";
export type DraftAuthority = "draft";
export type SaveSyncState =
  | "Saved on device"
  | "Syncing"
  | "Synced"
  | "Needs attention";

export interface RevisionRef {
  revision_id: PublicId;
  ordinal: number;
  tree_digest: Digest;
}

export interface SourceRef {
  source_id: PublicId;
  revision_id: PublicId;
  authority: Authority;
  label: string;
}

export interface ContractEnvelope<Name extends string> {
  contract_name: Name;
  contract_version: typeof CONTRACT_VERSION;
}

export type OperationName =
  | "campaign_create"
  | "session_start"
  | "session_capture"
  | "session_end"
  | "proposal_correct"
  | "proposal_reject"
  | "proposal_approve"
  | "provider_configure"
  | "provider_consent"
  | "projection_rebuild"
  | "backup_create";

export interface OperationRequest extends ContractEnvelope<"operation_request"> {
  request_id: PublicId;
  operation: OperationName;
  idempotency_key: PublicId;
  payload_digest: Digest;
  expected_revision: PublicId | null;
  expected_workflow_version: number | null;
  subject_id?: PublicId;
  intent_digest?: Digest;
}

export interface CampaignAtlas extends ContractEnvelope<"campaign_atlas"> {
  campaign: { campaign_id: PublicId; name: string; adapter_id: "mothership" };
  viewed_revision: RevisionRef;
  head_revision: PublicId;
  entities: ReadonlyArray<{
    entity_id: PublicId;
    entity_type: PublicId;
    name: string;
    authority: Exclude<Authority, "table_fact">;
  }>;
  connections: ReadonlyArray<{
    connection_id: PublicId;
    from_entity_id: PublicId;
    to_entity_id: PublicId;
    label: string;
    direction: "directed" | "undirected";
  }>;
  backlinks: ReadonlyArray<{
    source_entity_id: PublicId;
    target_entity_id: PublicId;
    connection_id: PublicId;
  }>;
  history: ReadonlyArray<{
    event_id: PublicId;
    revision_id: PublicId;
    authority: Exclude<Authority, "table_fact">;
    title: string;
  }>;
  comparison: {
    from_revision: PublicId;
    to_revision: PublicId;
    changes: ReadonlyArray<{
      subject_id: PublicId;
      change_type: "added" | "removed" | "changed" | "authority_changed";
    }>;
  };
}

export interface DraftProvenance {
  authority: DraftAuthority;
  revision: RevisionRef;
  sources: ReadonlyArray<SourceRef>;
}
