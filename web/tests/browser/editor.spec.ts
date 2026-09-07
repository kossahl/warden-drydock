import { expect, test } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";
import { campaigns, headRevision, overview, workflow, newestFiveHistory } from "../fixtures/atlas";

const editorRecord = {
  record_id: "record-one", record_type: "npc", displayed_name: "Station Keeper", status: "canon", authority: "canon",
  visibility: { audience: "warden" as const, warden_only: true as const }, fields: [{ field_id: "ownership", value: "campaign" }],
  sections: [{ section_id: "summary", body: "Keeps the synthetic station." }], connections: [], content_digest: "c".repeat(64),
};
const secondEditorRecord = { ...editorRecord, record_id: "record-two", record_type: "ship", displayed_name: "Legacy Ship", content_digest: "d".repeat(64) };
const editedRecord = { ...editorRecord, displayed_name: "Edited Station Keeper" };
const proposal = {
  contract_name: "editor_proposal_view", contract_version: 1, proposal_id: "proposal_editor", proposal_version: 1, campaign_id: "campaign_atlas",
  source_revision: headRevision, base_revision: headRevision, expected_campaign_head: headRevision, editor_workflow_version: 2,
  proposal_payload_digest: "d".repeat(64), mutation_kind: "edit", record_bindings: [{ campaign_id: "campaign_atlas", base_revision: headRevision, record_id: "record-one", record_digest: editorRecord.content_digest, expected_editor_workflow_version: 2 }],
  core_proposal: { proposal: { status: "needs_review" } }, diff: { diff_digest: "e".repeat(64), cards: [{ change_id: "change_editor", kind: "record_updated", subject_record_id: "record-one", before: editorRecord, after: editedRecord, property_changes: [{ property: "displayed_name", before: "Station Keeper", after: "Edited Station Keeper" }], connection: null, resolution: null, derived_backlinks: [] }], affected_record_count: 1, authority_changes: [], visibility_changes: [{ change_id: "visibility_editor", record_id: "record-one", before: { audience: "warden", warden_only: true }, after: { audience: "players", warden_only: false }, audience_broadens: true }], unresolved_reference_count: 0, impact_digest: null, summary: "edit" },
  impact_digest: null, impact_binding: null, resolutions: [], validation: { status: "passed", validation_digest: "f".repeat(64), error_count: 0, findings: [] }, authority_outcome: [], visibility_outcome: [{ change_id: "visibility_editor", record_id: "record-one", before: { audience: "warden", warden_only: true }, after: { audience: "players", warden_only: false }, audience_broadens: true }], publication: { status: "not_published", published_revision: null },
};

test("record editor submits an exact CSRF-bound proposal and approval dialog", async ({ page }) => {
  await installAtlasApi(page);
  const csrfRequests: string[] = [];
  let published = false;
  let campaignReads = 0;
  const publishedRevision = { revision_id: "revision_three", ordinal: 3, tree_digest: "1".repeat(64), immutable: true };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/campaigns") {
      campaignReads += 1;
      return route.fulfill({ json: published ? { ...campaigns, campaigns: campaigns.campaigns.map((item) => ({ ...item, head_revision: publishedRevision, projected_revision: publishedRevision })) } : campaigns });
    }
    if (published) {
      const binding = { campaign_id: "campaign_atlas", viewed_revision: publishedRevision, head_revision: publishedRevision };
      if (path.endsWith("/revisions/revision_three")) return route.fulfill({ json: { contract_name: "campaign_revision_view", contract_version: 2, campaign_id: "campaign_atlas", campaign_name: "Synthetic Atlas", adapter_id: "mothership", viewed_revision: publishedRevision, head_revision: publishedRevision.revision_id, records: [] } });
      if (path.endsWith("/atlas/overview")) return route.fulfill({ json: { ...overview, binding } });
      if (path.endsWith("/atlas/workflow-summary")) return route.fulfill({ json: { ...workflow, binding } });
      if (path.endsWith("/atlas/history")) return route.fulfill({ json: { ...newestFiveHistory, binding } });
    }
    if (path.endsWith("/records/record-one/editor") && request.method() === "GET") {
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: editorRecord }) });
    }
    if (path.endsWith("/records/record-one/proposals") && request.method() === "POST") {
      csrfRequests.push(request.headers()["x-csrf-token"] ?? "");
      return route.fulfill({ status: 201, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify(proposal) });
    }
    if (path.endsWith("/editor/proposals/proposal_editor/versions/1") && request.method() === "GET") {
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify(proposal) });
    }
    if (path.endsWith("/editor/proposals/proposal_editor/versions/1/approval") && request.method() === "POST") {
      published = true;
      csrfRequests.push(request.headers()["x-csrf-token"] ?? "");
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_proposal_approval_result", contract_version: 1, proposal: { proposal_id: "proposal_editor", proposal_version: 1 }, outcome: "published", published_revision: { revision_id: "revision_three", ordinal: 3, tree_digest: "1".repeat(64), immutable: true }, editor_workflow_version: 3 }) });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const editor = page.locator(".editor").filter({ hasText: "Edit record" });
  await expect(editor.getByRole("heading", { name: "Edit record" })).toBeVisible();
  await editor.getByLabel("Displayed name").fill("Edited keeper");
  await expect(editor.getByRole("button", { name: "Add field", exact: true })).toHaveCount(0);
  await expect(editor.getByRole("button", { name: "Add content section", exact: true })).toHaveCount(0);
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  await expect(editor.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  await expect(page).toHaveURL(/proposal=proposal_editor&version=1$/);
  await page.reload();
  await expect(editor.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  await editor.getByRole("button", { name: "Approve and publish exact proposal" }).click();
  await expect(page.getByRole("heading", { name: "Approve exact proposal" })).toBeFocused();
  await expect(page.getByRole("alert")).toHaveText(/broadens audience visibility/);
  await expect(page.getByRole("button", { name: "Approve and publish exact proposal", exact: true })).toBeDisabled();
  await page.getByRole("checkbox", { name: /I confirm the exact proposal/ }).check();
  await page.getByRole("button", { name: "Approve and publish exact proposal", exact: true }).click();
  await expect.poll(() => csrfRequests).toEqual(["browser-csrf", "browser-csrf"]);
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\?revision=revision_three$/);
  await expect(page.getByRole("complementary", { name: "Viewed revision" })).toHaveText(/revision_three · Head/);
  await expect(page.getByRole("link", { name: "Open head", exact: true })).toHaveCount(0);
  expect(campaignReads).toBe(3);
});

test("delayed editor reads cannot overwrite a different SPA record", async ({ page }) => {
  await installAtlasApi(page);
  let releaseOldRead!: () => void;
  const oldReadReleased = new Promise<void>((resolve) => { releaseOldRead = resolve; });
  const oldReadStarted = page.waitForRequest((request) => request.method() === "GET" && new URL(request.url()).pathname.endsWith("/records/record-one/editor"));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      await oldReadReleased;
      return route.fulfill({ headers: { "X-CSRF-Token": "browser-csrf" }, json: { contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: editorRecord } });
    }
    if (request.method() === "GET" && path.endsWith("/records/record-two/editor")) {
      return route.fulfill({ headers: { "X-CSRF-Token": "browser-csrf" }, json: { contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: secondEditorRecord } });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  await oldReadStarted;
  const relatedRecord = page.getByRole("link", { name: "Legacy Ship" }).first();
  await expect(relatedRecord).toBeVisible();
  const oldReadResponse = page.waitForResponse((response) => response.request().method() === "GET" && new URL(response.url()).pathname.endsWith("/records/record-one/editor"));
  await relatedRecord.click();
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-two\?revision=revision_two$/);
  const editor = page.locator(".editor").filter({ hasText: "Edit record" });
  await expect(editor.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(editor.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
  releaseOldRead();
  await oldReadResponse;
  await expect(editor.getByLabel("Record ID")).toHaveValue("record-two");
  await expect(editor.getByLabel("Displayed name")).toHaveValue("Legacy Ship");
});

test("rejection applies the returned workflow version to a fresh save", async ({ page }) => {
  await installAtlasApi(page);
  let editorReads = 0;
  let rejected = false;
  const submittedVersions: number[][] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      editorReads += 1;
      return route.fulfill({ headers: { "X-CSRF-Token": "browser-csrf" }, json: { contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: editorRecord } });
    }
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) {
      const body = request.postDataJSON();
      const versions = [body.binding.expected_editor_workflow_version, body.operation_request.expected_editor_workflow_version];
      submittedVersions.push(versions);
      return versions.every((version) => version === (rejected ? 3 : 1))
        ? route.fulfill({ status: 201, json: { ...proposal, editor_workflow_version: rejected ? 4 : 2 } })
        : route.fulfill({ status: 409, json: { error: { code: "workflow_conflict", category: "workflow_conflict" } } });
    }
    if (request.method() === "POST" && path.endsWith("/editor/proposals/proposal_editor/versions/1/rejection")) {
      rejected = true;
      return route.fulfill({ json: { contract_name: "editor_proposal_rejection_result", contract_version: 1, proposal: { proposal_id: "proposal_editor", proposal_version: 1 }, outcome: "rejected", editor_workflow_version: 3 } });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const editor = page.locator(".editor");
  await editor.getByLabel("Displayed name").fill("Edited keeper");
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  await editor.getByRole("button", { name: "Reject exact proposal" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Reject exact proposal" }).click();
  await expect(editor.getByText("Proposal rejected. No campaign revision changed.")).toBeVisible();
  await expect(editor.getByLabel("Displayed name")).toHaveValue("Edited keeper");
  await editor.getByLabel("Displayed name").fill("Fresh keeper");
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  await expect(editor.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  expect(submittedVersions).toEqual([[1, 1], [3, 3]]);
  expect(editorReads).toBe(1);
  await expect(page).toHaveURL(/records\/record-one\?revision=revision_two&proposal=proposal_editor&version=1$/);
});

test("same-head workflow conflict reloads the editor before retrying", async ({ page }) => {
  await installAtlasApi(page);
  let editorReads = 0;
  const submittedVersions: number[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/record-one/editor")) {
      editorReads += 1;
      return route.fulfill({ headers: { "X-CSRF-Token": "browser-csrf" }, json: { contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: editorReads, historical: false, editable: true, record: editorRecord } });
    }
    if (request.method() === "POST" && path.endsWith("/records/record-one/proposals")) {
      const version = request.postDataJSON().binding.expected_editor_workflow_version;
      submittedVersions.push(version);
      return version === 1
        ? route.fulfill({ status: 409, json: { error: { code: "workflow_conflict", category: "workflow_conflict" } } })
        : route.fulfill({ status: 201, json: proposal });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  const editor = page.locator(".editor");
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  await editor.getByRole("button", { name: "Reload current head" }).click();
  await expect(editor.getByRole("status")).toHaveText("Head · workflow 2");
  await expect(page).toHaveURL(/records\/record-one\?revision=revision_two$/);
  await editor.getByRole("button", { name: "Save as proposal" }).click();
  await expect(editor.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
  expect(editorReads).toBe(2);
  expect(submittedVersions).toEqual([1, 2]);
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

test("create correction uses the candidate ID, reads campaign context, and opens the created record", async ({ page }) => {
  await installAtlasApi(page);
  const editorReads: string[] = [];
  let initialBody: Record<string, any> | null = null;
  let correctionBody: Record<string, any> | null = null;
  const createdProposal = {
    ...proposal,
    proposal_id: "proposal_created",
    mutation_kind: "create" as const,
    record_bindings: [{ ...proposal.record_bindings[0], record_id: "record-created", record_digest: null }],
    diff: { ...proposal.diff, summary: "create", cards: [{ ...proposal.diff.cards[0], kind: "record_created", subject_record_id: "record-created" }] },
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path.endsWith("/records/campaign-main/editor")) {
      editorReads.push(path);
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_record_view", contract_version: 1, campaign_id: "campaign_atlas", viewed_revision: headRevision, head_revision: headRevision, editor_workflow_version: 1, historical: false, editable: true, record: { ...editorRecord, record_id: "campaign-main" } }) });
    }
    if (request.method() === "POST" && path.endsWith("/editor/records/proposals")) {
      initialBody = JSON.parse(request.postData() ?? "{}");
      return route.fulfill({ status: 201, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify(createdProposal) });
    }
    if (request.method() === "POST" && path.endsWith("/corrections")) {
      correctionBody = JSON.parse(request.postData() ?? "{}");
      return route.fulfill({ status: 201, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ ...createdProposal, proposal_version: 2 }) });
    }
    if (request.method() === "POST" && path.endsWith("/approval")) {
      return route.fulfill({ status: 200, headers: { "X-CSRF-Token": "browser-csrf" }, contentType: "application/json", body: JSON.stringify({ contract_name: "editor_proposal_approval_result", contract_version: 1, proposal: { proposal_id: "proposal_created", proposal_version: 2 }, outcome: "published", published_revision: { revision_id: "revision_three", ordinal: 3, tree_digest: "1".repeat(64), immutable: true }, editor_workflow_version: 3 }) });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/campaign_atlas/records/__new__?revision=revision_two");
  const editor = page.locator(".editor").filter({ hasText: "Create record" });
  await editor.getByLabel("Record ID").fill("record-created");
  await editor.getByRole("button", { name: "Add typed connection" }).click();
  const targetPicker = editor.getByRole("button", { name: "Target for connection_1: choose existing record" });
  await targetPicker.click();
  const targetDialog = page.getByRole("dialog");
  await targetDialog.getByLabel("Search existing records").fill("record-one");
  await targetDialog.getByRole("button", { name: "Search", exact: true }).click();
  await targetDialog.getByRole("option", { name: /record-one/ }).click();
  await editor.getByRole("button", { name: "Submit create proposal" }).click();
  await editor.getByRole("button", { name: "Create correction/rebase" }).click();
  await expect.poll(() => editorReads.length).toBe(2);
  expect(editorReads.every((path) => !path.endsWith("/records/new-record/editor"))).toBe(true);
  expect((initialBody as any)?.candidate?.connections?.[0]?.connection_id).toMatch(/^connection_[0-9]+$/);
  expect((initialBody as any)?.candidate?.connections?.[0]?.target_record_id).toBe("record-one");
  await editor.getByLabel("Displayed name").fill("Corrected created record");
  await editor.getByLabel("Context").fill("Corrected connection context.");
  await editor.getByRole("button", { name: "Submit correction/rebase" }).click();
  await expect.poll(() => (correctionBody as any)?.candidate?.record_id).toBe("record-created");
  await expect.poll(() => (correctionBody as any)?.candidate?.displayed_name).toBe("Corrected created record");
  expect((correctionBody as any)?.candidate?.connections?.[0]?.context).toBe("Corrected connection context.");
  await editor.getByRole("button", { name: "Approve and publish exact proposal" }).click();
  await expect(page.getByRole("button", { name: "Approve and publish exact proposal", exact: true })).toBeDisabled();
  await page.getByRole("checkbox", { name: /I confirm the exact proposal/ }).check();
  await page.getByRole("button", { name: "Approve and publish exact proposal", exact: true }).click();
  await expect(page).toHaveURL(/\/campaigns\/campaign_atlas\/records\/record-created\?revision=revision_three$/);
});
