import { expect, test, type Page, type Route } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";
import type { EditorProposal, EditorRecord, EditorRecordView, RevisionRef } from "../../src/editor/editorClient";
import { headRevision } from "../fixtures/atlas";

const oldRevision: RevisionRef = { revision_id: "revision_one", ordinal: 1, tree_digest: "b".repeat(64) };
const currentRevision: RevisionRef = { revision_id: "revision_three", ordinal: 3, tree_digest: "c".repeat(64) };
const laterRevision: RevisionRef = { revision_id: "revision_four", ordinal: 4, tree_digest: "d".repeat(64) };

const originalRecord: EditorRecord = {
  record_id: "record-one", record_type: "npc", displayed_name: "Station Keeper", status: "canon", authority: "canon",
  visibility: { audience: "warden", warden_only: true }, fields: [{ field_id: "ownership", value: "campaign" }],
  sections: [{ section_id: "summary", body: "Keeps the synthetic station." }], connections: [], content_digest: "a".repeat(64),
};
const currentRecord: EditorRecord = { ...originalRecord, displayed_name: "Current Head Keeper", content_digest: "c".repeat(64) };

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify(body) });

const view = (viewedRevision: RevisionRef, head: RevisionRef, record: EditorRecord, workflow = 1, historical = false): EditorRecordView => ({
  contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: viewedRevision,
  head_revision: head, editor_workflow_version: workflow, historical, editable: !historical, record,
});

const proposal = (base: RevisionRef, record: EditorRecord, version = 1, workflow = 1, id = "proposal_correction"): EditorProposal => ({
  contract_name: "editor_proposal_view", contract_version: 1, proposal_id: id, proposal_version: version,
  campaign_id: "campaign_atlas", source_revision: base, base_revision: base, expected_campaign_head: base,
  editor_workflow_version: workflow, proposal_payload_digest: "d".repeat(64), mutation_kind: "edit",
  record_bindings: [{ campaign_id: "campaign_atlas", base_revision: base, record_id: record.record_id, record_digest: record.content_digest, expected_editor_workflow_version: workflow }],
  core_proposal: { proposal: { status: "needs_review" } },
  diff: { diff_digest: "e".repeat(64), cards: [{ change_id: "change_editor", kind: "record_updated", subject_record_id: record.record_id, before: originalRecord, after: record, property_changes: [] }], affected_record_count: 1, authority_changes: [], visibility_changes: [], unresolved_reference_count: 0, impact_digest: null, summary: "edit" },
  impact_digest: null, impact_binding: null, resolutions: [],
  validation: { status: "passed", validation_digest: "f".repeat(64), error_count: 0, findings: [] },
  authority_outcome: [], visibility_outcome: [], publication: { status: "not_published", published_revision: null },
});

const editor = (page: Page) => page.locator(".editor").filter({ hasText: "Edit record" });

type CorrectionRequest = {
  candidate?: { displayed_name?: string };
  binding?: { base_revision?: RevisionRef; expected_editor_workflow_version?: number; record_digest?: string };
  operation_request?: { expected_revision?: string; expected_editor_workflow_version?: number };
};

test("canceling a correction restores the draft and keeps the original proposal version", async ({ page }) => {
  await installAtlasApi(page);
  let correctionRequests = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) return json(route, proposal(headRevision, { ...originalRecord, displayed_name: "Edited before proposal" } ), 201);
    if (request.method() === "POST" && path.endsWith("/corrections")) { correctionRequests += 1; return json(route, proposal(headRevision, originalRecord, 2), 201); }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByLabel("Displayed name").fill("Edited before proposal");
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  await expect(panel.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  await panel.getByRole("button", { name: "Create correction/rebase" }).click();
  await panel.getByLabel("Displayed name").fill("Unsubmitted correction");
  await panel.getByRole("button", { name: "Cancel correction" }).click();

  await expect(panel.getByLabel("Displayed name")).toHaveValue("Edited before proposal");
  await expect(panel.getByText(/proposal_correction.*, version 1/)).toBeVisible();
  expect(correctionRequests).toBe(0);
  await expect(panel.getByRole("button", { name: "Approve and publish exact proposal" })).toBeEnabled();
});

test("correction validation failure preserves entered content and blocks approval", async ({ page }) => {
  await installAtlasApi(page);
  let correctionBody: CorrectionRequest | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) return json(route, proposal(headRevision, originalRecord), 201);
    if (request.method() === "POST" && path.endsWith("/corrections")) {
      correctionBody = JSON.parse(request.postData() ?? "{}") as CorrectionRequest;
      return json(route, { error: { code: "proposal_validation_failure", category: "proposal_validation_failure" } }, 422);
    }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  await panel.getByRole("button", { name: "Create correction/rebase" }).click();
  await panel.getByLabel("Displayed name").fill("Entered correction survives validation");
  await panel.getByRole("button", { name: "Submit correction/rebase" }).click();

  await expect(panel.getByRole("heading", { name: "Editor error" })).toBeVisible();
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Entered correction survives validation");
  await expect(panel.getByRole("button", { name: "Approve and publish exact proposal" })).toBeDisabled();
  const failedRequest = correctionBody as unknown as CorrectionRequest;
  expect(failedRequest.candidate?.displayed_name).toBe("Entered correction survives validation");
});

test("stale correction reads cannot overwrite a record after SPA navigation", async ({ page }) => {
  await installAtlasApi(page);
  let editorReads = 0;
  let releaseCorrectionRead!: () => void;
  const correctionReadReleased = new Promise<void>((resolve) => { releaseCorrectionRead = resolve; });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      editorReads += 1;
      if (editorReads > 1) await correctionReadReleased;
      return json(route, view(headRevision, headRevision, originalRecord));
    }
    if (request.method() === "GET" && path.endsWith("/records/record-two/editor")) {
      return json(route, view(headRevision, headRevision, { ...originalRecord, record_id: "record-two", record_type: "ship", displayed_name: "Legacy Ship", content_digest: "b".repeat(64) }));
    }
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) return json(route, proposal(headRevision, { ...originalRecord, displayed_name: "Edited before proposal" }), 201);
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  const correctionReadStarted = page.waitForRequest((request) => request.method() === "GET" && new URL(request.url()).pathname.endsWith("/records/record-one/editor"));
  await panel.getByRole("button", { name: "Create correction/rebase" }).click();
  await correctionReadStarted;
  await page.getByRole("link", { name: "Legacy Ship" }).first().click();
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
  releaseCorrectionRead();
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
});

test("stale proposal responses cannot install a proposal after SPA navigation", async ({ page }) => {
  await installAtlasApi(page);
  let releaseProposal!: () => void;
  let proposalStarted!: () => void;
  const proposalReleased = new Promise<void>((resolve) => { releaseProposal = resolve; });
  const proposalRequestStarted = new Promise<void>((resolve) => { proposalStarted = resolve; });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "GET" && path.endsWith("/records/record-two/editor")) return json(route, view(headRevision, headRevision, { ...originalRecord, record_id: "record-two", record_type: "ship", displayed_name: "Legacy Ship", content_digest: "b".repeat(64) }));
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) {
      proposalStarted();
      await proposalReleased;
      return json(route, proposal(headRevision, { ...originalRecord, displayed_name: "Edited before navigation" }), 201);
    }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByLabel("Displayed name").fill("Edited before navigation");
  const oldProposalResponse = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/records/record-one/proposals"));
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  await proposalRequestStarted;
  await page.getByRole("link", { name: "Legacy Ship" }).first().click();
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
  releaseProposal();
  await oldProposalResponse;
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
  await expect(panel.getByRole("heading", { name: "Exact proposal review" })).toHaveCount(0);
});

test("stale decision responses cannot change a different SPA record", async ({ page }) => {
  await installAtlasApi(page);
  let releaseApproval!: () => void;
  let approvalStarted!: () => void;
  const approvalReleased = new Promise<void>((resolve) => { releaseApproval = resolve; });
  const approvalRequestStarted = new Promise<void>((resolve) => { approvalStarted = resolve; });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "GET" && path.endsWith("/records/record-two/editor")) return json(route, view(headRevision, headRevision, { ...originalRecord, record_id: "record-two", record_type: "ship", displayed_name: "Legacy Ship", content_digest: "b".repeat(64) }));
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) return json(route, proposal(headRevision, { ...originalRecord, displayed_name: "Edited before approval" }), 201);
    if (request.method() === "POST" && path.endsWith("/approval")) {
      approvalStarted();
      await approvalReleased;
      return json(route, { contract_name: "editor_proposal_approval_result", contract_version: 1, proposal: { proposal_id: "proposal_correction", proposal_version: 1 }, outcome: "published", published_revision: { revision_id: "revision_three", ordinal: 3, tree_digest: "3".repeat(64), immutable: true }, editor_workflow_version: 2 });
    }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  await panel.getByRole("button", { name: "Approve and publish exact proposal" }).click();
  await page.getByRole("checkbox", { name: /I confirm the exact proposal/ }).check();
  await page.getByRole("dialog").getByRole("button", { name: "Approve and publish exact proposal" }).click();
  await approvalRequestStarted;
  await page.evaluate(() => {
    history.pushState(null, "", "/campaigns/campaign_atlas/records/record-two?revision=revision_two");
    window.dispatchEvent(new Event("drydock:navigate"));
  });
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  releaseApproval();
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(panel.getByRole("heading", { name: "Exact proposal review" })).toHaveCount(0);
});

test("stale removal-impact responses cannot switch the editor after SPA navigation", async ({ page }) => {
  await installAtlasApi(page);
  let releaseImpact!: () => void;
  let impactStarted!: () => void;
  const impactReleased = new Promise<void>((resolve) => { releaseImpact = resolve; });
  const impactRequestStarted = new Promise<void>((resolve) => { impactStarted = resolve; });
  const impact = {
    contract_name: "editor_removal_impact", contract_version: 1,
    binding: { campaign_id: "campaign_atlas", base_revision: headRevision, record_id: "record-one", record_digest: originalRecord.content_digest, expected_editor_workflow_version: 1 },
    impact_digest: "i".repeat(64), record: originalRecord, outgoing_connections: [], incoming_references: [],
    backlink_policy: "server_derived_from_typed_connections",
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "GET" && path.endsWith("/records/record-two/editor")) return json(route, view(headRevision, headRevision, { ...originalRecord, record_id: "record-two", record_type: "ship", displayed_name: "Legacy Ship", content_digest: "b".repeat(64) }));
    if (request.method() === "GET" && path.endsWith("/records/record-one/removal-impact")) {
      impactStarted();
      await impactReleased;
      return json(route, impact);
    }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByRole("button", { name: "Load removal impact" }).click();
  await impactRequestStarted;
  await page.getByRole("link", { name: "Legacy Ship" }).first().click();
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  releaseImpact();
  await expect(panel.getByRole("heading", { name: "Removal impact and resolutions" })).toHaveCount(0);
});

test("superseded proposal review is read-only", async ({ page }) => {
  await installAtlasApi(page);
  const superseded = { ...proposal(headRevision, originalRecord), core_proposal: { proposal: { status: "rejected" } } };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "GET" && path.endsWith("/editor/proposals/proposal_correction/versions/1")) return json(route, superseded);
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two&proposal=proposal_correction&version=1");
  const panel = editor(page);
  await expect(panel.getByText(/Status: rejected/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Reject exact proposal" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Create correction\/rebase" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Approve and publish exact proposal" })).toBeDisabled();
});

test("stale correction responses cannot install a proposal after SPA navigation", async ({ page }) => {
  await installAtlasApi(page);
  let releaseCorrection!: () => void;
  let correctionStarted!: () => void;
  const correctionReleased = new Promise<void>((resolve) => { releaseCorrection = resolve; });
  const correctionRequestStarted = new Promise<void>((resolve) => { correctionStarted = resolve; });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) return json(route, view(headRevision, headRevision, originalRecord));
    if (request.method() === "GET" && path.endsWith("/records/record-two/editor")) return json(route, view(headRevision, headRevision, { ...originalRecord, record_id: "record-two", record_type: "ship", displayed_name: "Legacy Ship", content_digest: "b".repeat(64) }));
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) return json(route, proposal(headRevision, { ...originalRecord, displayed_name: "Edited before correction" }), 201);
    if (request.method() === "POST" && path.endsWith("/corrections")) {
      correctionStarted();
      await correctionReleased;
      return json(route, proposal(headRevision, { ...originalRecord, displayed_name: "Corrected proposal" }, 2), 201);
    }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  await panel.getByRole("button", { name: "Create correction/rebase" }).click();
  await panel.getByLabel("Displayed name").fill("Pending correction");
  const oldCorrectionResponse = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/corrections"));
  await panel.getByRole("button", { name: "Submit correction/rebase" }).click();
  await correctionRequestStarted;
  await page.getByRole("link", { name: "Legacy Ship" }).first().click();
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
  releaseCorrection();
  await oldCorrectionResponse;
  await expect(panel.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
  await expect(panel.getByRole("heading", { name: "Exact proposal review" })).toHaveCount(0);
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
});

test("historical proposal URLs keep review and correction available", async ({ page }) => {
  await installAtlasApi(page);
  const correctedProposal = { ...proposal(headRevision, { ...currentRecord, displayed_name: "Corrected historical" }, 2, 8, "proposal_historical"), correction_of: { proposal_id: "proposal_historical", proposal_version: 1 } };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      if (path.includes(`/revisions/${currentRevision.revision_id}/`)) return json(route, view(currentRevision, currentRevision, currentRecord, 8));
      return json(route, view(oldRevision, currentRevision, originalRecord, 7, true));
    }
    if (request.method() === "GET" && path.endsWith("/editor/proposals/proposal_historical/versions/1")) return json(route, proposal(oldRevision, { ...originalRecord, displayed_name: "Historical proposal" }, 1, 7, "proposal_historical"));
    if (request.method() === "GET" && path.endsWith("/editor/proposals/proposal_historical/versions/2")) return json(route, correctedProposal);
    if (request.method() === "POST" && path.endsWith("/corrections")) return json(route, correctedProposal, 201);
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_one&proposal=proposal_historical&version=1");
  const panel = editor(page);
  await expect(panel.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  await expect(panel.getByLabel("Displayed name")).toBeDisabled();
  await panel.getByRole("button", { name: "Create correction/rebase" }).click();
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Current Head Keeper");
  await expect(panel.getByLabel("Displayed name")).toBeEnabled();
  await panel.getByLabel("Displayed name").fill("Corrected historical");
  await panel.getByRole("button", { name: "Submit correction/rebase" }).click();
  await expect(panel.getByRole("link", { name: /proposal_historical/ })).toHaveAttribute("href", /revision=revision_one&proposal=proposal_historical&version=1$/);
  await panel.getByRole("link", { name: /proposal_historical/ }).click();
  await expect(page).toHaveURL(/revision=revision_one&proposal=proposal_historical&version=1$/);
  await expect(panel.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
});

test("stale correction binds to the loaded head even if a later head appears before submit", async ({ page }) => {
  await installAtlasApi(page);
  let latestHead = currentRevision;
  let correctionBody: CorrectionRequest | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      const revisionId = path.split("/").at(-4);
      if (revisionId === oldRevision.revision_id) return json(route, view(oldRevision, currentRevision, originalRecord, 7));
      if (revisionId === currentRevision.revision_id) return json(route, view(currentRevision, latestHead, currentRecord, 8));
      return json(route, view(headRevision, headRevision, originalRecord));
    }
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) return json(route, proposal(oldRevision, { ...originalRecord, displayed_name: "Prior stale proposal" }, 1, 7), 201);
    if (request.method() === "POST" && path.endsWith("/corrections")) {
      correctionBody = JSON.parse(request.postData() ?? "{}") as CorrectionRequest;
      return json(route, { error: { code: "stale_revision", category: "stale_revision" } }, 409);
    }
    return route.fallback();
  });

  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const panel = editor(page);
  await panel.getByRole("button", { name: "Save as proposal" }).click();
  await panel.getByRole("button", { name: "Create correction/rebase" }).click();
  await expect(panel.getByLabel("Displayed name")).toHaveValue("Current Head Keeper");
  latestHead = laterRevision;
  await panel.getByLabel("Displayed name").fill("Manually reapplied change");
  await panel.getByRole("button", { name: "Submit correction/rebase" }).click();

  await expect(panel.getByRole("heading", { name: "Editor error" })).toBeVisible();
  const staleRequest = correctionBody as unknown as CorrectionRequest;
  expect(staleRequest.operation_request?.expected_revision).toBe(currentRevision.revision_id);
  expect(staleRequest.operation_request?.expected_editor_workflow_version).toBe(8);
  expect(staleRequest.binding?.base_revision?.revision_id).toBe(currentRevision.revision_id);
  expect(staleRequest.binding?.expected_editor_workflow_version).toBe(8);
  expect(staleRequest.binding?.record_digest).toBe(currentRecord.content_digest);
  expect(staleRequest.candidate?.displayed_name).toBe("Manually reapplied change");
  expect(staleRequest.operation_request?.expected_revision).not.toBe(laterRevision.revision_id);
});
