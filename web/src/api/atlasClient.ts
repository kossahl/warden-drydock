import type { AtlasAuthority, AtlasCampaignCollection, AtlasHistoryCollection, AtlasNeighborhood, AtlasOverview, AtlasRecordDetail, AtlasRecordLibraryResult, AtlasRevisionRef, AtlasStatusFilter, AtlasWorkflowSummary } from "../contracts/v1";
import type { CampaignRevisionView } from "../contracts/v2";
import { ApiError } from "./client";

export interface AtlasRevisionQuery { revision_id: string; revision_ordinal: number; tree_digest: string; }
export interface AtlasRecordsQuery extends AtlasRevisionQuery {
  q: string;
  types: ReadonlyArray<string>;
  authorities: ReadonlyArray<AtlasAuthority>;
  statuses: ReadonlyArray<AtlasStatusFilter>;
  cursor?: string;
}
export interface AtlasHistoryQuery extends AtlasRevisionQuery {
  subject_record_id?: string;
  cursor?: string;
  direction?: "forward" | "backward";
  limit?: 5 | 50;
}
export interface AtlasApi {
  campaigns(): Promise<AtlasCampaignCollection>;
  resolveRevision(campaignId: string, revisionId: string): Promise<AtlasRevisionRef>;
  overview(campaignId: string, revision: AtlasRevisionQuery): Promise<AtlasOverview>;
  records(campaignId: string, query: AtlasRecordsQuery): Promise<AtlasRecordLibraryResult>;
  record(campaignId: string, recordId: string, revision: AtlasRevisionQuery): Promise<AtlasRecordDetail>;
  neighborhood(campaignId: string, recordId: string, revision: AtlasRevisionQuery): Promise<AtlasNeighborhood>;
  history(campaignId: string, query: AtlasHistoryQuery): Promise<AtlasHistoryCollection>;
  workflow(campaignId: string, revision: AtlasRevisionQuery): Promise<AtlasWorkflowSummary>;
}

async function requestAtlasJson<T>(path: string): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => null) as { error?: { code?: string } } | null;
  if (!response.ok) throw new ApiError(response.status, body?.error?.code ?? "request_failed");
  return body as T;
}
const atlasPath = (value: string) => encodeURIComponent(value);
const appendRevision = (params: URLSearchParams, revision: AtlasRevisionQuery) => {
  params.append("revision_id", revision.revision_id);
  params.append("revision_ordinal", String(revision.revision_ordinal));
  params.append("tree_digest", revision.tree_digest);
};
const withQuery = (path: string, params: URLSearchParams) => `${path}?${params.toString()}`;

export function atlasOverviewUrl(campaignId: string, revision: AtlasRevisionQuery) {
  const params = new URLSearchParams(); appendRevision(params, revision);
  return withQuery(`/campaigns/${atlasPath(campaignId)}/atlas/overview`, params);
}
export function atlasRecordsUrl(campaignId: string, query: AtlasRecordsQuery) {
  const params = new URLSearchParams(); appendRevision(params, query);
  params.append("q", query.q);
  query.types.forEach((value) => params.append("type", value));
  query.authorities.forEach((value) => params.append("authority", value));
  query.statuses.forEach((value) => params.append("status", value));
  params.append("limit", "50");
  if (query.cursor) params.append("cursor", query.cursor);
  return withQuery(`/campaigns/${atlasPath(campaignId)}/atlas/records`, params);
}
export function atlasHistoryUrl(campaignId: string, query: AtlasHistoryQuery) {
  const params = new URLSearchParams(); appendRevision(params, query);
  if (query.subject_record_id) params.append("subject_record_id", query.subject_record_id);
  params.append("limit", String(query.limit ?? 50));
  if (query.cursor) params.append("cursor", query.cursor);
  if (query.direction) params.append("direction", query.direction);
  return withQuery(`/campaigns/${atlasPath(campaignId)}/atlas/history`, params);
}
function atlasBoundUrl(path: string, revision: AtlasRevisionQuery) {
  const params = new URLSearchParams(); appendRevision(params, revision);
  return withQuery(path, params);
}

export const httpAtlasApi: AtlasApi = {
  campaigns: () => requestAtlasJson("/campaigns"),
  async resolveRevision(campaignId, revisionId) {
    const value = await requestAtlasJson<CampaignRevisionView>(`/campaigns/${atlasPath(campaignId)}/revisions/${atlasPath(revisionId)}`);
    return { revision_id: value.viewed_revision.revision_id, ordinal: value.viewed_revision.ordinal, tree_digest: value.viewed_revision.tree_digest };
  },
  overview: (campaignId, revision) => requestAtlasJson(atlasOverviewUrl(campaignId, revision)),
  records: (campaignId, query) => requestAtlasJson(atlasRecordsUrl(campaignId, query)),
  record: (campaignId, recordId, revision) => requestAtlasJson(atlasBoundUrl(`/campaigns/${atlasPath(campaignId)}/atlas/records/${atlasPath(recordId)}`, revision)),
  neighborhood: (campaignId, recordId, revision) => {
    const params = new URLSearchParams(); appendRevision(params, revision); params.append("depth", "1"); params.append("limit", "50");
    return requestAtlasJson(withQuery(`/campaigns/${atlasPath(campaignId)}/atlas/records/${atlasPath(recordId)}/neighborhood`, params));
  },
  history: (campaignId, query) => requestAtlasJson(atlasHistoryUrl(campaignId, query)),
  workflow: (campaignId, revision) => requestAtlasJson(atlasBoundUrl(`/campaigns/${atlasPath(campaignId)}/atlas/workflow-summary`, revision)),
};
