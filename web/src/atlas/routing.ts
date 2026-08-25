import type { AtlasAuthority, AtlasStatusFilter } from "../contracts/v2";

export type AtlasRouteKind = "overview" | "records" | "record" | "history" | "invalid";
export interface AtlasRoute {
  kind: AtlasRouteKind;
  campaignId: string;
  recordId?: string;
  revisionId: string | null;
  q: string;
  type: string | null;
  authority: AtlasAuthority | null;
  status: AtlasStatusFilter | null;
  cursor: string | null;
  relationshipCursor: string | null;
  generationCursor: string | null;
  proposalCursor: string | null;
}

const authorities = new Set<AtlasAuthority>(["preparation", "canon", "revealed"]);
const statuses = new Set<AtlasStatusFilter>(["idea", "draft", "review", "canon", "revealed", "archived", "accepted", "missing", "unknown"]);
const safeDecode = (value: string) => { try { return decodeURIComponent(value); } catch { return ""; } };

export function parseAtlasRoute(location: string): AtlasRoute {
  const url = new URL(location, "http://drydock.local");
  const parts = url.pathname.split("/").filter(Boolean).map(safeDecode);
  const campaignId = parts[0] === "campaigns" ? parts[1] ?? "" : "";
  let kind: AtlasRouteKind = "invalid";
  let recordId: string | undefined;
  if (parts.length === 2) kind = "overview";
  else if (parts.length === 3 && parts[2] === "records") kind = "records";
  else if (parts.length === 4 && parts[2] === "records") { kind = "record"; recordId = parts[3]; }
  else if (parts.length === 3 && parts[2] === "history") kind = "history";
  const authority = url.searchParams.get("authority");
  const status = url.searchParams.get("status");
  return {
    kind,
    campaignId,
    recordId,
    revisionId: url.searchParams.get("revision"),
    q: url.searchParams.get("q") ?? "",
    type: url.searchParams.get("type"),
    authority: authority && authorities.has(authority as AtlasAuthority) ? authority as AtlasAuthority : null,
    status: status && statuses.has(status as AtlasStatusFilter) ? status as AtlasStatusFilter : null,
    cursor: url.searchParams.get("cursor"),
    relationshipCursor: url.searchParams.get("relationship_cursor"),
    generationCursor: url.searchParams.get("generation_cursor"),
    proposalCursor: url.searchParams.get("proposal_cursor"),
  };
}

export interface AtlasUrlState {
  revisionId: string;
  q?: string;
  type?: string | null;
  authority?: AtlasAuthority | null;
  status?: AtlasStatusFilter | null;
  cursor?: string | null;
  relationshipCursor?: string | null;
  generationCursor?: string | null;
  proposalCursor?: string | null;
}

export function atlasHref(campaignId: string, destination: "overview" | "records" | "history", state: AtlasUrlState) {
  const path = destination === "overview" ? `/campaigns/${encodeURIComponent(campaignId)}` : `/campaigns/${encodeURIComponent(campaignId)}/${destination}`;
  return addState(path, state, destination === "records");
}

export function recordHref(campaignId: string, recordId: string, state: AtlasUrlState) {
  return addState(`/campaigns/${encodeURIComponent(campaignId)}/records/${encodeURIComponent(recordId)}`, state, true);
}

function addState(path: string, state: AtlasUrlState, includeLibrary: boolean) {
  const params = new URLSearchParams();
  params.set("revision", state.revisionId);
  if (includeLibrary) {
    if (state.q) params.set("q", state.q);
    if (state.type) params.set("type", state.type);
    if (state.authority) params.set("authority", state.authority);
    if (state.status) params.set("status", state.status);
    if (state.cursor) params.set("cursor", state.cursor);
  }
  if (state.relationshipCursor) params.set("relationship_cursor", state.relationshipCursor);
  if (state.generationCursor) params.set("generation_cursor", state.generationCursor);
  if (state.proposalCursor) params.set("proposal_cursor", state.proposalCursor);
  return `${path}?${params.toString()}`;
}
