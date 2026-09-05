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

const view = (viewedRevision: RevisionRef, head: RevisionRef, record: EditorRecord, workflow = 1): EditorRecordView => ({
  contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: viewedRevision,
  head_revision: head, editor_workflow_version: workflow, historical: false, editable: true, record,
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
