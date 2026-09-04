import { expect, test } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";
import { headRevision } from "../fixtures/atlas";

const editorRecord = {
  record_id: "record-one", record_type: "npc", displayed_name: "Station Keeper", status: "canon", authority: "canon",
  visibility: { audience: "warden" as const, warden_only: true as const }, fields: [{ field_id: "ownership", value: "campaign" }],
  sections: [{ section_id: "summary", body: "Keeps the synthetic station." }], connections: [], content_digest: "c".repeat(64),
};
const editedRecord = { ...editorRecord, displayed_name: "Edited Station Keeper" };
const proposal = {
  contract_name: "editor_proposal_view", contract_version: 1, proposal_id: "proposal_editor", proposal_version: 1, campaign_id: "campaign_atlas",
  source_revision: headRevision, base_revision: headRevision, expected_campaign_head: headRevision, editor_workflow_version: 2,
  proposal_payload_digest: "d".repeat(64), mutation_kind: "edit", record_bindings: [{ campaign_id: "campaign_atlas", base_revision: headRevision, record_id: "record-one", record_digest: editorRecord.content_digest, expected_editor_workflow_version: 2 }],
  core_proposal: { proposal: { status: "needs_review" } }, diff: { diff_digest: "e".repeat(64), cards: [{ change_id: "change_editor", kind: "record_updated", subject_record_id: "record-one", before: editorRecord, after: editedRecord, property_changes: [{ property: "displayed_name", before: "Station Keeper", after: "Edited Station Keeper" }], connection: null, resolution: null, derived_backlinks: [] }], affected_record_count: 1, authority_changes: [], visibility_changes: [], unresolved_reference_count: 0, impact_digest: null, summary: "edit" },
  impact_digest: null, impact_binding: null, resolutions: [], validation: { status: "passed", validation_digest: "f".repeat(64), error_count: 0, findings: [] }, authority_outcome: [], visibility_outcome: [], publication: { status: "not_published", published_revision: null },
};

test("record editor submits an exact CSRF-bound proposal and approval dialog", async ({ page }) => {
  await installAtlasApi(page);
  const csrfRequests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/records/record-one/editor") && request.method() === "GET") {
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: editorRecord }) });
    }
    if (path.endsWith("/records/record-one/proposals") && request.method() === "POST") {
      csrfRequests.push(request.headers()["x-csrf-token"] ?? "");
      return route.fulfill({ status: 201, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify(proposal) });
    }
    if (path.endsWith("/editor/proposals/proposal_editor/versions/1/approval") && request.method() === "POST") {
      csrfRequests.push(request.headers()["x-csrf-token"] ?? "");
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_proposal_approval_result", contract_version: 1, proposal: { proposal_id: "proposal_editor", proposal_version: 1 }, outcome: "published", published_revision: { revision_id: "revision_three", ordinal: 3, tree_digest: "1".repeat(64), immutable: true }, editor_workflow_version: 3 }) });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const editor = page.locator(".editor").filter({ hasText: "Edit record" });
  await expect(editor.getByRole("heading", { name: "Edit record" })).toBeVisible();
  await editor.getByRole("button", { name: "Add field" }).click();
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  await expect(editor.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  await editor.getByRole("button", { name: "Approve and publish exact proposal" }).click();
  await expect(page.getByRole("heading", { name: "Approve exact proposal" })).toBeFocused();
  await page.getByRole("button", { name: "Approve and publish" }).click();
  await expect.poll(() => csrfRequests).toEqual(["browser-csrf", "browser-csrf"]);
});

test("editor load errors do not steal Atlas record-heading focus", async ({ page }) => {
  await installAtlasApi(page);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "editor_unavailable" } }) });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const heading = page.getByRole("heading", { level: 1, name: "Station Keeper" });
  await expect(heading).toBeFocused();
  await expect(page.getByRole("alert").filter({ hasText: "Editor unavailable" })).toBeVisible();
  await expect(heading).toBeFocused();
});

test("editor action errors focus the editor error without reducing accessibility", async ({ page }) => {
  await installAtlasApi(page);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: editorRecord }) });
    }
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) {
      return route.fulfill({ status: 422, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ error: { code: "proposal_validation_failure", category: "proposal_validation_failure" } }) });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const editor = page.locator(".editor").filter({ hasText: "Edit record" });
  await expect(editor.getByRole("heading", { name: "Edit record" })).toBeVisible();
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  const errorHeading = editor.getByRole("heading", { name: "Editor error" });
  await expect(errorHeading).toBeVisible();
  await expect(errorHeading).toBeFocused();
  await expect(editor.getByLabel("Displayed name")).toBeEnabled();
});
