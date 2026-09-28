import { expect, test, type Page, type Route } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";
import { campaigns, headRevision } from "../fixtures/atlas";
import type { LiveSessionView, ProviderReadiness } from "../../src/contracts/v2";
import type { EditorProposal, EditorRecord } from "../../src/editor/editorClient";

const ready: ProviderReadiness = { contract_name: "provider_readiness_response", contract_version: 2, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: "9".repeat(64), ai_available: true };
type LiveTestServer = { head: string; initialCampaignHead?: string; campaignReads: number; failNextCampaignRead?: boolean; session: LiveSessionView | null; failCaptures: boolean; offline: boolean; interruptStream: boolean; capturePosts: number; endPosts: number; startHeads: string[]; generationRequests: Array<Record<string, unknown>>; endRequirements: Array<Array<{ device_id: string; operation_id: string }>>; barrierEvents: string[]; proposalBodies: Array<{ endpoint: string; body: Record<string, unknown> }>; captureGate?: { started: () => void; response: Promise<void> } };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const liveRoute = (sessionId: string, revision = "revision_two") => `/campaigns/campaign_atlas/live?revision=${revision}&session=${sessionId}`;
const keysDeep = (value: unknown): string[] => Array.isArray(value) ? value.flatMap(keysDeep) : value && typeof value === "object" ? Object.entries(value).flatMap(([key, nested]) => [key, ...keysDeep(nested)]) : [];

function sessionView(sessionId: string, controllerId: string, revision: string): LiveSessionView {
  return {
    contract_name: "live_session_view", contract_version: 2, session_id: sessionId, campaign_id: "campaign_atlas", base_revision: revision, reported_head_revision: revision, workflow_version: 1,
    controller: { epoch: 1, controller_id: controllerId, mode: "controller" }, mode: "active", events: [], acknowledgements: [], end_barrier: null,
    overlay: { overlay_id: "overlay_alpha", authority: "non_canon", base_revision: revision, confirmed_fact_ids: [], question_ids: [] },
  };
}

const editorRecord: EditorRecord = { record_id: "record-one", record_type: "npc", displayed_name: "Station Keeper", ownership: "campaign", status: "canon", authority: "canon", visibility: { audience: "warden", warden_only: true }, fields: [{ field_id: "ownership", value: "campaign" }], sections: [{ section_id: "summary", body: "Keeps the station." }], connections: [], content_digest: "c".repeat(64) };
const editorProposal = (version: number, record: EditorRecord, correctionOf?: { proposal_id: string; proposal_version: number }): EditorProposal => ({
  contract_name: "editor_proposal_view", contract_version: 1, proposal_id: "proposal_live_capture", proposal_version: version,
  campaign_id: "campaign_atlas", source_revision: headRevision, base_revision: headRevision, expected_campaign_head: headRevision,
  editor_workflow_version: 1, proposal_payload_digest: "d".repeat(64), mutation_kind: "edit", ...(correctionOf ? { correction_of: correctionOf } : {}),
  record_bindings: [{ campaign_id: "campaign_atlas", base_revision: headRevision, record_id: record.record_id, record_digest: editorRecord.content_digest, expected_editor_workflow_version: 1 }],
  core_proposal: { proposal: { status: "needs_review" } },
  diff: { diff_digest: "e".repeat(64), cards: [{ change_id: "change_live", kind: "record_updated", subject_record_id: record.record_id, before: editorRecord, after: record, property_changes: [] }], affected_record_count: 1, authority_changes: [], visibility_changes: [], unresolved_reference_count: 0, impact_digest: null, summary: "edit" },
  impact_digest: null, impact_binding: null, resolutions: [], validation: { status: "passed", validation_digest: "f".repeat(64), error_count: 0, findings: [] }, authority_outcome: [], visibility_outcome: [], publication: { status: "not_published", published_revision: null },
});

async function installLive(page: Page, server: LiveTestServer) {
  await installAtlasApi(page, { readiness: ready });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/campaigns" && request.method() === "GET") {
      server.campaignReads += 1;
      if (server.failNextCampaignRead) { server.failNextCampaignRead = false; return route.abort("connectionfailed"); }
      const campaignHead = server.campaignReads === 1 ? server.initialCampaignHead ?? server.head : server.head;
      const collection = { ...campaigns, campaigns: campaigns.campaigns.map((item) => ({ ...item, head_revision: { ...item.head_revision, revision_id: campaignHead }, projected_revision: { ...item.head_revision, revision_id: campaignHead } })) };
      return json(route, collection);
    }
    if (path.endsWith("/revisions/revision_four") && request.method() === "GET") return json(route, { contract_name: "campaign_revision_view", contract_version: 2, campaign_id: "campaign_atlas", campaign_name: "Synthetic Atlas", adapter_id: "mothership", viewed_revision: { ...headRevision, revision_id: "revision_four" }, head_revision: "revision_four", records: [] });
    if (path === "/api/v1/campaigns/campaign_atlas/live/session" && request.method() === "GET") {
      if (server.offline) return route.abort("connectionfailed");
      if (!server.session) return json(route, { contract_name: "error_response", contract_version: 2, error: { category: "not_found", code: "not_found", stage: "live_read", request_id: "request_alpha", retryable: false } }, 404);
      server.session.reported_head_revision = server.head;
      return json(route, server.session);
    }
    if (path === "/api/v1/campaigns/campaign_atlas/live/session" && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, any>;
      server.startHeads.push(body.head_revision);
      server.session = sessionView(body.session_id, body.controller_id, body.head_revision);
      return json(route, server.session, 201);
    }
    if (path.endsWith("/live/session/takeover") && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, any>;
      if (!server.session || body.controller_epoch !== server.session.controller.epoch) return json(route, { error: { code: "stale_controller", category: "stale_controller" } }, 409);
      server.session.controller = { epoch: server.session.controller.epoch + 1, controller_id: body.controller_id, mode: "controller" };
      server.session.workflow_version += 1;
      return json(route, server.session);
    }
    if (path.endsWith("/live/session/captures") && request.method() === "POST") {
      server.capturePosts += 1;
      if (server.offline || server.failCaptures) return route.abort("connectionfailed");
      const body = request.postDataJSON() as Record<string, any>;
      if (server.captureGate) { server.captureGate.started(); await server.captureGate.response; }
      if (!server.session || body.controller_id !== server.session.controller.controller_id || body.controller_epoch !== server.session.controller.epoch) return json(route, { contract_name: "error_response", contract_version: 2, error: { category: "stale_controller", code: "stale_controller", stage: "live_capture", request_id: "request_stale", retryable: false } }, 409);
      const event = { event_id: body.event_id, event_type: body.capture_type, device_id: body.device_id, operation_id: body.operation_id, device_order: body.device_order, payload_digest: body.operation_request.payload_digest, base_revision: server.session.base_revision, grounding_eligible: body.capture_type === "confirmed_fact", record_id: body.record_id };
      if (!server.session.events.some((item) => item.operation_id === event.operation_id)) server.session.events = [...server.session.events, event];
      server.session.acknowledgements = [...server.session.acknowledgements, { device_id: body.device_id, operation_id: body.operation_id, payload_digest: body.operation_request.payload_digest, outcome: "accepted" }];
      if (event.grounding_eligible) server.session.overlay.confirmed_fact_ids = [...server.session.overlay.confirmed_fact_ids, event.event_id];
      else server.session.overlay.question_ids = [...server.session.overlay.question_ids, event.event_id];
      server.barrierEvents.push("capture_acknowledged");
      server.session.workflow_version += 1;
      return json(route, { contract_name: "live_capture_result", contract_version: 2, outcome: "accepted", campaign_id: body.campaign_id, session_id: body.session_id, event_id: body.event_id, device_id: body.device_id, operation_id: body.operation_id, session: server.session });
    }
    if (path.endsWith("/live/session/end") && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, any>;
      server.barrierEvents.push("end_requested");
      server.endPosts += 1; server.endRequirements.push(body.required_operation_ids);
      const acknowledged = server.session?.acknowledgements.map(({ device_id, operation_id }) => ({ device_id, operation_id })) ?? [];
      const required = body.required_operation_ids as Array<{ device_id: string; operation_id: string }>;
      if (!server.session || required.length !== acknowledged.length || required.some((item) => !acknowledged.some((receipt) => receipt.device_id === item.device_id && receipt.operation_id === item.operation_id))) return json(route, { error: { category: "live_barrier_conflict", code: "live_barrier_conflict" } }, 409);
      server.session.mode = "ended_review_pending";
      server.session.workflow_version += 1;
      server.session.end_barrier = { end_device_id: body.device_id, end_operation_id: body.operation_id, required_operation_ids: required, acknowledged_operation_ids: required, ready_for_proposal: true };
      return json(route, server.session);
    }
    if (path.endsWith("/revisions/revision_two/generations") && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      server.generationRequests.push(body);
      const generationId = String(body.generation_id);
      return json(route, { contract_name: "generation_view", contract_version: 2, generation_id: generationId, campaign_id: "campaign_atlas", source_revision: body.source_revision, action: body.action, context: body.context, session_id: body.session_id, draft_authority: "draft", status: "pending", sources: [{ source_id: "record-one", authority: "canon", revision_id: body.source_revision, order: 1, excerpt: "Pinned campaign source", excerpt_digest: "1".repeat(64) }], source_set_digest: "2".repeat(64), last_sequence: 0, terminal_content: null, terminal_content_digest: null }, 202);
    }
    if (path.endsWith("/events") && request.method() === "GET") {
      if (server.interruptStream) { server.interruptStream = false; return route.abort("connectionfailed"); }
      const id = path.split("/").at(-2)!;
      const event = (sequence: number, fragment: string) => ({ contract_name: "generation_event", contract_version: 2, generation_id: id, sequence, event_type: "delta", draft_fragment: fragment, retryable: null });
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: [event(1, "First "), event(2, "then second.")].map((item) => `id: ${item.sequence}\nevent: delta\ndata: ${JSON.stringify(item)}\n\n`).join("") });
    }
    if (/\/generations\/[^/]+$/.test(path) && request.method() === "GET") {
      const request = server.generationRequests.at(-1)!;
      return json(route, { contract_name: "generation_view", contract_version: 2, generation_id: path.split("/").at(-1), campaign_id: "campaign_atlas", source_revision: request.source_revision, action: request.action, context: request.context, session_id: request.session_id, draft_authority: "draft", status: "complete", sources: [{ source_id: "record-one", authority: "canon", revision_id: request.source_revision, order: 1, excerpt: "Pinned campaign source", excerpt_digest: "1".repeat(64) }], source_set_digest: "2".repeat(64), last_sequence: 2, terminal_content: "First then second.", terminal_content_digest: "3".repeat(64) });
    }
    if (path.endsWith("/records/record-one/editor") && request.method() === "GET") return json(route, { contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: { record_id: "record-one", record_type: "npc", displayed_name: "Station Keeper", ownership: "campaign", status: "canon", authority: "canon", visibility: { audience: "warden", warden_only: true }, fields: [{ field_id: "ownership", value: "campaign" }], sections: [{ section_id: "summary", body: "Keeps the station." }], connections: [], content_digest: "c".repeat(64) } });
    if (path.endsWith("/records/record-one/proposals") && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      server.proposalBodies.push({ endpoint: "proposal", body });
      return json(route, editorProposal(1, { ...editorRecord, displayed_name: "Reviewed live update" }), 201);
    }
    if (/\/editor\/proposals\/proposal_live_capture\/versions\/\d+\/corrections$/.test(path) && request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      server.proposalBodies.push({ endpoint: "correction", body });
      const candidate = body.candidate as EditorRecord;
      return json(route, editorProposal(2, candidate, { proposal_id: "proposal_live_capture", proposal_version: 1 }), 201);
    }
    if (/\/editor\/proposals\/proposal_live_capture\/versions\/\d+$/.test(path) && request.method() === "GET") {
      const version = Number(path.match(/versions\/(\d+)$/)?.[1]);
      return json(route, version === 1 ? editorProposal(1, { ...editorRecord, displayed_name: "Reviewed live update" }) : editorProposal(2, { ...editorRecord, displayed_name: "Corrected live update" }, { proposal_id: "proposal_live_capture", proposal_version: 1 }));
    }
    return route.fallback();
  });
}

const serverState = (): LiveTestServer => ({ head: "revision_two", campaignReads: 0, session: null, failCaptures: false, offline: false, interruptStream: false, capturePosts: 0, endPosts: 0, startHeads: [], generationRequests: [], endRequirements: [], barrierEvents: [], proposalBodies: [] });
async function startSession(page: Page, server: LiveTestServer) {
  await page.goto("/campaigns/campaign_atlas/live?revision=revision_two");
  await page.getByRole("button", { name: "Start live session" }).click();
  await expect(page.getByText("This tab controls the session")).toBeVisible();
  await expect(page.locator("#atlas-content main")).toHaveCount(0);
  await expect(page.getByRole("main")).toHaveCount(1);
  return server.session!.session_id;
}

test("start binds to the refreshed campaign head", async ({ page }) => {
  const server = serverState();
  server.head = "revision_three";
  server.initialCampaignHead = "revision_two";
  await installLive(page, server);
  const firstCampaignRead = page.waitForResponse(async (response) => {
    if (new URL(response.url()).pathname !== "/api/v1/campaigns" || response.request().method() !== "GET") return false;
    const body = await response.json() as typeof campaigns;
    return body.campaigns.some((item) => item.campaign_id === "campaign_atlas" && item.head_revision.revision_id === "revision_two");
  });
  const refreshedCampaignRead = page.waitForResponse(async (response) => {
    if (new URL(response.url()).pathname !== "/api/v1/campaigns" || response.request().method() !== "GET") return false;
    const body = await response.json() as typeof campaigns;
    return body.campaigns.some((item) => item.campaign_id === "campaign_atlas" && item.head_revision.revision_id === "revision_three");
  }, { timeout: 10000 });
  await page.goto("/campaigns/campaign_atlas/live");
  await Promise.all([firstCampaignRead, refreshedCampaignRead]);
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  const readsBeforeStart = server.campaignReads;
  server.head = "revision_four";
  await page.getByRole("button", { name: "Start live session" }).click();
  await expect.poll(() => server.campaignReads).toBe(readsBeforeStart + 1);
  await expect.poll(() => server.startHeads).toEqual(["revision_four"]);
  await expect(page.getByText("This tab controls the session")).toBeVisible();
  expect(server.startHeads).toEqual(["revision_four"]);
});

test("start fails closed when the on-demand campaign head read fails", async ({ page }) => {
  const server = serverState();
  server.head = "revision_three";
  server.initialCampaignHead = "revision_two";
  await installLive(page, server);
  const firstCampaignRead = page.waitForResponse(async (response) => {
    if (new URL(response.url()).pathname !== "/api/v1/campaigns" || response.request().method() !== "GET") return false;
    const body = await response.json() as typeof campaigns;
    return body.campaigns.some((item) => item.campaign_id === "campaign_atlas" && item.head_revision.revision_id === "revision_two");
  });
  const refreshedCampaignRead = page.waitForResponse(async (response) => {
    if (new URL(response.url()).pathname !== "/api/v1/campaigns" || response.request().method() !== "GET") return false;
    const body = await response.json() as typeof campaigns;
    return body.campaigns.some((item) => item.campaign_id === "campaign_atlas" && item.head_revision.revision_id === "revision_three");
  }, { timeout: 10000 });
  await page.goto("/campaigns/campaign_atlas/live");
  await Promise.all([firstCampaignRead, refreshedCampaignRead]);
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  const readsBeforeStart = server.campaignReads;
  server.failNextCampaignRead = true;
  await page.getByRole("button", { name: "Start live session" }).click();
  await expect(page.getByRole("alert")).toContainText("Could not start the live session");
  expect(server.campaignReads).toBe(readsBeforeStart + 1);
  expect(server.startHeads).toEqual([]);
});
async function saveFact(page: Page, text: string, record = "record-one") {
  await page.getByLabel("What happened or remains unresolved?").fill(text);
  await page.getByLabel("Affected record ID, if known").fill(record);
  await page.getByRole("button", { name: "Save capture" }).click();
}

test("offline capture survives IndexedDB reload and changes from device saved to acknowledged", async ({ page }) => {
  const server = serverState(); server.failCaptures = true;
  await installLive(page, server);
  const sessionId = await startSession(page, server);
  await saveFact(page, "Airlock opened.");
  await expect(page.getByText("Saved on device", { exact: true })).toBeVisible();
  expect(server.session?.events).toHaveLength(0);
  await page.reload();
  await expect(page.getByText("Airlock opened.")).toBeVisible();
  await expect(page.getByText(sessionId)).toBeVisible();
  await expect(page.getByText("Observer · read only")).toBeVisible();
  await expect(page.getByText("Saved on device", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Take over control" }).click();
  await expect(page.getByText("This tab controls the session")).toBeVisible();
  server.failCaptures = false;
  await page.getByRole("button", { name: "Sync saved work" }).click();
  await expect(page.getByText("Synced", { exact: true })).toBeVisible();
  expect(server.session?.events).toHaveLength(1);
});

test("second tab observes, explicitly takes over, and old controller receives stale writer feedback", async ({ page, context }) => {
  const server = serverState(); await installLive(page, server);
  const sessionId = await startSession(page, server);
  let releaseCapture!: () => void;
  let markCaptureStarted!: () => void;
  const captureRequestStarted = new Promise<void>((resolve) => { markCaptureStarted = resolve; });
  const captureResponse = new Promise<void>((resolve) => { releaseCapture = resolve; });
  server.captureGate = { started: markCaptureStarted, response: captureResponse };
  const observer = await context.newPage(); await installLive(observer, server);
  await observer.goto(liveRoute(sessionId));
  await expect(observer.getByText("Observer · read only")).toBeVisible();
  await expect(observer.getByRole("button", { name: "Save capture" })).toBeDisabled();
  await saveFact(page, "Late entry from stale tab.");
  await captureRequestStarted;
  await observer.getByRole("button", { name: "Take over control" }).click();
  await expect(observer.getByText("This tab controls the session")).toBeVisible();
  releaseCapture();
  await expect(page.getByRole("alert")).toContainText("lost live control");
  await expect(page.getByText("Saved on device", { exact: true })).toBeVisible();
  expect(server.session?.events).toHaveLength(0);
  await observer.reload();
  await expect(observer.getByText("Observer · read only")).toBeVisible();
  await expect(observer.getByRole("alert")).toContainText("lost live control");
  await observer.getByRole("button", { name: "Take over control" }).click();
  await expect(observer.getByText("This tab controls the session")).toBeVisible();
  await expect(observer.getByRole("alert")).toHaveCount(0);
  await observer.close();
});

test("observer cannot sync saved work until taking over control", async ({ page, context }) => {
  const server = serverState(); server.failCaptures = true;
  await installLive(page, server);
  const sessionId = await startSession(page, server);
  await saveFact(page, "A pending observer sync test capture.");
  await expect(page.getByText("Saved on device", { exact: true })).toBeVisible();
  await expect.poll(() => server.capturePosts).toBe(1);

  const observer = await context.newPage(); await installLive(observer, server);
  await observer.goto(liveRoute(sessionId));
  await expect(observer.getByText("Observer · read only")).toBeVisible();
  await expect(observer.getByText("A pending observer sync test capture.")).toBeVisible();
  const syncButton = observer.getByRole("button", { name: "Sync saved work" });
  await expect(syncButton).toBeDisabled();
  expect(server.capturePosts).toBe(1);

  await observer.getByRole("button", { name: "Take over control" }).click();
  await expect(observer.getByText("This tab controls the session")).toBeVisible();
  server.failCaptures = false;
  await syncButton.click();
  await expect(observer.getByText("Synced", { exact: true })).toBeVisible();
  await expect.poll(() => server.capturePosts).toBe(2);
  expect(server.session?.events).toHaveLength(1);
  await observer.close();
});

test("head changes are announced without rebinding generation; interrupted stream resumes without duplicate text", async ({ page }) => {
  const server = serverState(); server.interruptStream = true;
  await installLive(page, server);
  await startSession(page, server);
  server.head = "revision_three";
  await expect(page.getByText(/Campaign head is now revision_three/)).toBeVisible({ timeout: 16000 });
  await page.getByRole("radio", { name: "Generate" }).check();
  await page.getByLabel("Generation brief").fill("Describe the signal.");
  await page.getByRole("button", { name: "Submit generate" }).click();
  await expect(page.getByRole("button", { name: /Resume stream after event 0/ })).toBeVisible();
  await page.getByRole("button", { name: /Resume stream after event 0/ }).click();
  await expect(page.getByText("First then second.", { exact: true })).toBeVisible();
  expect(server.generationRequests).toHaveLength(1);
  expect(server.generationRequests[0].source_revision).toBe("revision_two");
  expect(server.generationRequests[0].session_id).toBe(server.session?.session_id);
});

test("Ask and Check show sources while unresolved questions remain outside confirmed grounding", async ({ page }) => {
  const server = serverState(); await installLive(page, server); await startSession(page, server);
  await page.getByRole("radio", { name: "Unresolved question" }).check();
  await saveFact(page, "Is the signal a warning?");
  await expect(page.getByText("Is the signal a warning?")).toBeVisible();
  await expect(page.getByText("Synced", { exact: true })).toBeVisible();
  const question = server.session!.events[0];
  expect(question.grounding_eligible).toBe(false);
  expect(server.session!.overlay.question_ids).toContain(question.event_id);
  expect(server.session!.overlay.confirmed_fact_ids).not.toContain(question.event_id);

  await page.getByRole("textbox", { name: "Question" }).fill("What did the crew hear?");
  await page.getByRole("button", { name: "Submit ask" }).click();
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  await expect(page.getByText("Pinned campaign source")).toBeVisible();
  await page.getByRole("radio", { name: "Check" }).check();
  await page.getByLabel("Claim to check").fill("The signal came from the airlock.");
  await page.getByRole("button", { name: "Submit check" }).click();
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  await expect(page.getByText("Pinned campaign source")).toBeVisible();
  expect(server.generationRequests.map(({ action }) => action)).toEqual(["ask", "check"]);
  expect(server.generationRequests.every(({ session_id }) => session_id === server.session?.session_id)).toBe(true);
  expect(server.session!.overlay.confirmed_fact_ids).not.toContain(question.event_id);
});

test("end stays local until the exact capture set is acknowledged by the server barrier", async ({ page }) => {
  const server = serverState(); server.failCaptures = true;
  await installLive(page, server); await startSession(page, server);
  await saveFact(page, "Door opened.");
  page.once("dialog", async (dialog) => { expect(dialog.message()).toContain("Unsynced captures"); await dialog.accept(); });
  await page.getByRole("button", { name: "End session" }).click();
  // The end button requires confirmation; the intent remains local while its capture is unacknowledged.
  await expect(page.getByText(/Saved on device/).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "End intent saved" })).toBeDisabled();
  expect(server.endPosts).toBe(0);
  server.failCaptures = false;
  await page.getByRole("button", { name: "Sync saved work" }).click();
  await expect(page.getByText(/End state: Ended - review pending/)).toBeVisible();
  await expect.poll(() => server.endPosts).toBe(1);
  expect(server.endRequirements[0]).toHaveLength(1);
  expect(server.session?.end_barrier?.ready_for_proposal).toBe(true);
  expect(server.barrierEvents).toEqual(["capture_acknowledged", "end_requested"]);
  expect(server.endRequirements[0]).toEqual(server.session?.acknowledgements.map(({ device_id, operation_id }) => ({ device_id, operation_id })));
});

test("persisted end intent locks writes when synchronization fails", async ({ page }) => {
  const server = serverState(); await installLive(page, server); await startSession(page, server);
  await page.evaluate(() => {
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request: () => Promise.reject(new Error("forced_sync_failure")) },
    });
  });
  page.once("dialog", async (dialog) => { await dialog.accept(); });
  await page.getByRole("button", { name: "End session" }).click();

  await expect(page.getByRole("alert")).toContainText("Sync could not finish (forced_sync_failure)");
  await expect(page.getByText(/End state: Saved on device/)).toBeVisible();
  await expect(page.getByLabel("What happened or remains unresolved?")).toBeDisabled();
  await expect(page.getByRole("radio", { name: "Ask" })).toBeDisabled();
  await expect(page.getByRole("radio", { name: "Check" })).toBeDisabled();
  await expect(page.getByRole("radio", { name: "Generate" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Submit ask" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "End intent saved" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Sync saved work" })).toBeEnabled();
  expect(server.endPosts).toBe(0);

  await page.addInitScript(() => {
    const originalGetAll = IDBIndex.prototype.getAll;
    const state = { pending: 0 };
    let released = false;
    const waiting: Array<() => void> = [];
    Object.defineProperty(window, "__endLookupBarrier", {
      configurable: true,
      value: {
        state,
        release() {
          released = true;
          waiting.splice(0).forEach((resume) => resume());
        },
      },
    });
    IDBIndex.prototype.getAll = function (this: IDBIndex, ...args: Parameters<typeof originalGetAll>) {
      const request = originalGetAll.apply(this, args);
      if (this.objectStore.name !== "ends") return request;
      return new Proxy(request, {
        get(target, property) { return Reflect.get(target, property, target); },
        set(target, property, value) {
          if (property === "onsuccess" && typeof value === "function") {
            return Reflect.set(target, property, function (this: IDBRequest, event: Event) {
              state.pending += 1;
              if (released) value.call(this, event);
              else waiting.push(() => value.call(this, event));
            }, target);
          }
          return Reflect.set(target, property, value, target);
        },
      });
    };
  });
  await page.reload();
  await page.waitForFunction(() => (window as Window & { __endLookupBarrier?: { state: { pending: number } } }).__endLookupBarrier?.state.pending! > 0);
  await expect(page.getByText("Observer · read only")).toBeVisible();
  await page.getByRole("button", { name: "Take over control" }).click();
  await expect(page.getByText("This tab controls the session")).toBeVisible();
  await page.waitForFunction(() => (window as Window & { __endLookupBarrier?: { state: { pending: number } } }).__endLookupBarrier?.state.pending! > 1);
  await expect(page.getByRole("button", { name: "Save capture" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Submit ask" })).toBeDisabled();
  await page.locator("#live-prompt").evaluate((formControl) => {
    const textarea = formControl as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(textarea, "Must not be submitted before local rehydration.");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator("#live-prompt")).toHaveValue("Must not be submitted before local rehydration.");
  await page.locator("#live-prompt").evaluate((formControl) => {
    formControl.closest("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  expect(server.generationRequests).toHaveLength(0);
  await page.evaluate(() => (window as Window & { __endLookupBarrier?: { release: () => void } }).__endLookupBarrier?.release());
  await expect(page.getByText(/End state: Saved on device/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Save capture" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Submit ask" })).toBeDisabled();
});

test("end storage failure leaves the live session unlocked", async ({ page }) => {
  const server = serverState(); await installLive(page, server); await startSession(page, server);
  await page.evaluate(() => {
    Object.defineProperty(window, "indexedDB", {
      configurable: true,
      value: { open: () => { throw new Error("forced_local_queue_failure"); } },
    });
  });
  page.once("dialog", async (dialog) => { await dialog.accept(); });
  await page.getByRole("button", { name: "End session" }).click();

  await expect(page.getByRole("alert")).toContainText("End intent was not saved (forced_local_queue_failure)");
  await expect(page.getByRole("button", { name: "Save capture" })).toBeEnabled();
  for (const action of ["Ask", "Check", "Generate"] as const) {
    const choice = page.getByRole("radio", { name: action });
    await expect(choice).toBeEnabled();
    await choice.check();
    await expect(page.getByRole("button", { name: `Submit ${action.toLowerCase()}` })).toBeEnabled();
  }
  await expect(page.getByRole("button", { name: "End session" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "End intent saved" })).toHaveCount(0);
  await expect(page.getByText(/End state:/)).toHaveCount(0);
  expect(server.endPosts).toBe(0);
});

test("end lookup failure keeps a reloaded live session locked", async ({ page }) => {
  const server = serverState(); await installLive(page, server); await startSession(page, server);
  await page.addInitScript(() => {
    const originalGetAll = IDBIndex.prototype.getAll;
    IDBIndex.prototype.getAll = function (this: IDBIndex, ...args: Parameters<typeof originalGetAll>) {
      if (this.objectStore.name === "ends") throw new Error("forced_end_lookup_failure");
      return originalGetAll.apply(this, args);
    };
  });
  await page.reload();
  await expect(page.getByText("Observer · read only")).toBeVisible();
  await page.getByRole("button", { name: "Take over control" }).click();
  await expect(page.getByText("This tab controls the session")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Local live state could not be loaded (forced_end_lookup_failure)");
  await expect(page.getByRole("button", { name: "Save capture" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Submit ask" })).toBeDisabled();
  expect(server.generationRequests).toHaveLength(0);
});

test("end barrier and post-session review include acknowledged captures from another tab", async ({ page }) => {
  const server = serverState(); await installLive(page, server); await startSession(page, server);
  const remoteEvent = { event_id: "event_remote", event_type: "confirmed_fact" as const, device_id: "device_remote", operation_id: "operation_remote", device_order: 1, payload_digest: "4".repeat(64), base_revision: "revision_two", grounding_eligible: true, record_id: "record-one" };
  server.session!.events = [remoteEvent];
  server.session!.acknowledgements = [{ device_id: remoteEvent.device_id, operation_id: remoteEvent.operation_id, payload_digest: remoteEvent.payload_digest, outcome: "accepted" }];
  server.session!.workflow_version += 1;
  await page.reload();
  await expect(page.getByText("Observer · read only")).toBeVisible();
  await page.getByRole("button", { name: "Take over control" }).click();
  page.once("dialog", async (dialog) => { await dialog.accept(); });
  await page.getByRole("button", { name: "End session" }).click();
  await expect(page.getByRole("button", { name: "Review affected record" })).toBeVisible();
  expect(server.endRequirements[0]).toEqual([{ device_id: "device_remote", operation_id: "operation_remote" }]);
  await page.getByRole("button", { name: "Review affected record" }).click();
  await expect(page).toHaveURL(/capture_operation=operation_remote.*affected_record=record-one/);
  await expect(page.getByRole("region", { name: "Live capture provenance" })).toContainText("operation_remote");
});

test("post-barrier record review preserves provenance through proposal correction, including at 320px by keyboard", async ({ page }) => {
  const server = serverState(); await installLive(page, server);
  const sessionId = await startSession(page, server);
  await page.setViewportSize({ width: 320, height: 720 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await saveFact(page, "The station lights failed.");
  await expect(page.getByText("Synced", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open affected record in editor" })).toBeVisible();
  const capturedOperation = server.session!.acknowledgements[0].operation_id;
  await page.getByRole("button", { name: "Generate Draft from this fact" }).click();
  await page.getByLabel("Generation brief").fill("Describe the darkness.");
  await page.getByRole("button", { name: "Submit generate" }).click();
  await expect(page.getByText("First then second.", { exact: true })).toBeVisible();
  page.once("dialog", async (dialog) => { expect(dialog.message()).toContain("End this live session"); await dialog.accept(); });
  await page.getByRole("button", { name: "End session" }).click();
  await expect(page.getByRole("status").filter({ hasText: "End state: Ended - review pending" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Review affected record" })).toBeVisible();
  expect(server.barrierEvents).toEqual(["capture_acknowledged", "end_requested"]);
  await page.getByRole("button", { name: "Review affected record" }).click();
  await expect(page).toHaveURL(new RegExp(`live_session=${sessionId}.*source_revision=revision_two.*capture_operation=${capturedOperation}.*affected_record=record-one`));
  const provenance = page.getByRole("region", { name: "Live capture provenance" });
  await expect(provenance).toContainText(sessionId);
  await expect(provenance).toContainText("revision_two");
  await expect(provenance).toContainText(capturedOperation);
  await expect(provenance).toContainText("record-one");
  await page.reload();
  const reloadedProvenance = page.getByRole("region", { name: "Live capture provenance" });
  await expect(reloadedProvenance).toContainText(capturedOperation);
  await page.getByLabel("Displayed name").fill("Reviewed live update");
  await page.getByRole("button", { name: "Save as proposal" }).click();
  await expect(page.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  await page.getByRole("button", { name: "Create correction/rebase" }).click();
  await page.getByLabel("Displayed name").fill("Corrected live update");
  await page.getByRole("button", { name: "Submit correction/rebase" }).click();
  await expect(page.getByText(/proposal_live_capture, version 2/)).toBeVisible();
  const correctedProvenance = page.getByRole("region", { name: "Live capture provenance" });
  for (const value of [sessionId, "revision_two", capturedOperation, "record-one"]) await expect(correctedProvenance).toContainText(value);
  expect(server.proposalBodies.map(({ endpoint }) => endpoint)).toEqual(["proposal", "correction"]);
  for (const { body } of server.proposalBodies) {
    const serialized = JSON.stringify(body);
    for (const field of ["live_session", "source_revision", "capture_operation", "affected_record"]) expect(keysDeep(body)).not.toContain(field);
    expect(serialized).not.toContain(sessionId);
    expect(serialized).not.toContain(capturedOperation);
    expect(body).toHaveProperty("binding.base_revision.revision_id", "revision_two");
    expect(body).toHaveProperty("binding.record_id", "record-one");
  }
  await page.reload();
  await expect(page.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  for (const value of [sessionId, "revision_two", capturedOperation, "record-one"]) await expect(page.getByRole("region", { name: "Live capture provenance" })).toContainText(value);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await expect(page.getByRole("button", { name: "Close editor" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBeTruthy();
});
