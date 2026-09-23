import { expect, test } from "@playwright/test";
import type { EditorProposal, EditorRecord, EditorRecordView } from "../../src/editor/editorClient";

// Exercise the shipped client against the real HTTP/engine/revision services.
// Mock responses cannot catch incompatible connection IDs or publication drift.
test("editor publishes reviewed section corrections and resolves multiple references", async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto("/");
  await page.getByLabel("Campaign name").fill("Editor regression campaign");
  const creation = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/api/v1/campaigns"));
  await page.getByRole("button", { name: "Create campaign", exact: true }).click();
  const created = await creation;
  expect(created.status()).toBe(201);
  const campaign = await created.json() as { campaign_id: string; head_revision: string };
  let revision = campaign.head_revision;
  const recordUrl = (recordId: string, at = revision) => `/campaigns/${campaign.campaign_id}/records/${recordId}?revision=${at}`;
  const editor = page.locator(".editor");

  async function read(recordId: string, at = revision): Promise<EditorRecordView> {
    const response = await page.request.get(`/api/v1/campaigns/${campaign.campaign_id}/revisions/${at}/records/${recordId}/editor`);
    expect(response.status()).toBe(200);
    return response.json() as Promise<EditorRecordView>;
  }

  async function submit(button: string, suffix = "/proposals"): Promise<EditorProposal> {
    const pending = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith(suffix));
    await editor.getByRole("button", { name: button, exact: true }).click();
    const response = await pending;
    expect(response.status(), await response.text()).toBe(201);
    await expect(editor.getByRole("heading", { name: "Exact proposal review" })).toBeVisible();
    return response.json() as Promise<EditorProposal>;
  }

  async function approve() {
    await editor.getByRole("button", { name: "Approve and publish exact proposal", exact: true }).click();
    const approvalDialog = page.getByRole("dialog", { name: "Approve exact proposal" });
    const pending = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/approval"));
    await expect(approvalDialog.getByRole("button", { name: "Approve and publish exact proposal", exact: true })).toBeDisabled();
    await approvalDialog.getByRole("checkbox", { name: /I confirm the exact proposal/ }).check();
    await approvalDialog.getByRole("button", { name: "Approve and publish exact proposal", exact: true }).click();
    const response = await pending;
    expect(response.status(), await response.text()).toBe(200);
    const result = await response.json() as { published_revision: { revision_id: string } };
    revision = result.published_revision.revision_id;
    await expect(page).toHaveURL(new RegExp(`revision=${revision}$`));
  }

  async function startCreate(recordId: string) {
    await page.goto(recordUrl("__new__"));
    await editor.getByLabel("Record ID", { exact: true }).fill(recordId);
    await editor.getByLabel("Displayed name", { exact: true }).fill(recordId);
    await editor.getByLabel("Status", { exact: true }).selectOption("draft");
    await editor.getByLabel("summary", { exact: true }).fill("Original summary.\n");
  }

  await startCreate("npc-target");
  await submit("Submit create proposal");
  await approve();

  await startCreate("npc-source");
  await editor.getByLabel("wants", { exact: true }).fill("Original second section.\n");
  for (let index = 0; index < 2; index += 1) {
    await editor.getByRole("button", { name: "Add typed connection", exact: true }).click();
    const connection = editor.locator(".editor-connection").nth(index);
    const pickerButton = connection.getByRole("button", { name: new RegExp(`Target for connection_${index + 1}: choose existing record`) });
    await pickerButton.click();
    const targetDialog = page.locator(".record-picker-dialog");
    await expect(targetDialog.getByLabel("Search existing records")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(targetDialog).toBeHidden();
    await expect(pickerButton).toBeFocused();
    await pickerButton.click();
    await expect(targetDialog.getByLabel("Search existing records")).toBeFocused();
    await targetDialog.getByLabel("Search existing records").fill("npc-target");
    await targetDialog.getByRole("button", { name: "Search", exact: true }).click();
    await targetDialog.getByRole("option", { name: /npc-target/ }).click();
    if (index === 1) await connection.getByLabel("Relationship", { exact: true }).selectOption("supports");
    await connection.getByLabel("Context", { exact: true }).fill(`Source reference ${index + 1}.`);
  }
  await submit("Submit create proposal");
  await approve();
  const originalRevision = revision;
  const original = await read("npc-source");
  expect(original.record.connections).toHaveLength(2);

  await page.goto(recordUrl("npc-source"));
  await page.getByRole("button", { name: "Edit this record", exact: true }).click();
  await editor.getByLabel("summary", { exact: true }).fill("First\nsecond\nthird\nfourth\nfifth\nsixth\n");
  await editor.getByLabel("wants", { exact: true }).fill("Replacement second section.\n");
  const priorProposal = await submit("Save as proposal");
  await editor.getByRole("button", { name: "Create correction/rebase", exact: true }).click();
  await editor.getByLabel("Displayed name", { exact: true }).fill("Corrected source");
  await expect(editor.getByRole("button", { name: "Approve and publish exact proposal", exact: true })).toBeDisabled();
  const proposal = await submit("Submit correction/rebase", "/corrections");
  expect(proposal.proposal_id).toBe(priorProposal.proposal_id);
  expect(proposal.proposal_version).toBe(priorProposal.proposal_version + 1);
  expect((await read("npc-source")).record.displayed_name).toBe("npc-source");
  const reviewed = proposal.diff.cards.find((card) => card.kind === "record_updated")!.after as EditorRecordView["record"];
  await approve();
  expect((await read("npc-source")).record.displayed_name).toBe("Corrected source");
  expect((await read("npc-source")).record.sections).toEqual(reviewed.sections);
  expect((await read("npc-source", originalRevision)).record).toEqual(original.record);

  await page.goto(recordUrl("npc-target"));
  await page.getByRole("button", { name: "Edit this record", exact: true }).click();
  await editor.getByRole("button", { name: "Load removal impact", exact: true }).click();
  await expect(editor.getByRole("button", { name: "Cancel removal", exact: true })).toBeVisible();
  await editor.getByRole("button", { name: "Cancel removal", exact: true }).click();
  await expect(editor.getByRole("button", { name: "Load removal impact", exact: true })).toBeVisible();
  await editor.getByRole("button", { name: "Load removal impact", exact: true }).click();
  const resolutions = editor.getByLabel(/^Resolution for/);
  await expect(resolutions).toHaveCount(2);
  const removalSubmit = editor.getByRole("button", { name: "Submit removal proposal", exact: true });
  await expect(editor.getByLabel("Displayed name", { exact: true })).toBeDisabled();
  await expect(editor.getByLabel("summary", { exact: true })).toBeDisabled();
  await expect(editor.getByRole("button", { name: "Add typed connection", exact: true })).toBeDisabled();
  for (const resolution of await resolutions.all()) await expect(resolution).toHaveValue("");
  await expect(removalSubmit).toBeDisabled();
  await resolutions.nth(0).selectOption("remove_reference");
  await resolutions.nth(0).selectOption("redirect");
  await expect(removalSubmit).toBeDisabled();
  await resolutions.nth(0).selectOption("remove_reference");
  await expect(resolutions.nth(1)).toHaveValue("");
  await expect(removalSubmit).toBeDisabled();
  await resolutions.nth(1).selectOption("remove_reference");
  await expect(removalSubmit).toBeEnabled();
  const removal = await submit("Submit removal proposal", "/removal-proposals");
  expect(removal.diff.cards.filter((card) => card.kind === "reference_resolution")).toHaveLength(2);
  expect(removal.record_bindings.map((binding) => binding.record_id).sort()).toEqual(["npc-source", "npc-target"]);
  for (const resolution of await resolutions.all()) await expect(resolution).toBeDisabled();
  await editor.getByRole("button", { name: "Approve and publish exact proposal", exact: true }).click();
  const removalApproval = page.locator(".editor-dialog");
  await expect(removalApproval).toContainText("This record disappears only from the new approved revision");
  await expect(removalApproval).toContainText("Historical revisions retain it");
  await expect(removalApproval).toContainText("npc-source");
  await removalApproval.getByRole("button", { name: "Cancel", exact: true }).click();
  await editor.getByRole("button", { name: "Create correction/rebase", exact: true }).click();
  await expect(resolutions.first()).toBeEnabled();
  await resolutions.first().selectOption("redirect");
  await editor.getByRole("button", { name: /^Replacement target for .*choose existing record$/ }).click();
  const replacementDialog = page.locator(".record-picker-dialog");
  await replacementDialog.getByLabel("Search existing records").fill("npc-source");
  await replacementDialog.getByRole("button", { name: "Search", exact: true }).click();
  await replacementDialog.getByRole("option", { name: /npc-source/ }).click();
  await editor.getByRole("button", { name: "Cancel correction", exact: true }).click();
  for (const resolution of await resolutions.all()) {
    await expect(resolution).toHaveValue("remove_reference");
    await expect(resolution).toBeDisabled();
  }
  await approve();
  expect((await read("npc-source")).record.connections).toEqual([]);
  expect((await read("npc-source", originalRevision)).record.connections).toHaveLength(2);
});

test("live editor preserves context after backend relationship validation failure", async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto("/");
  await page.getByLabel("Campaign name").fill("Live editor validation regression campaign");
  const creation = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/api/v1/campaigns"));
  await page.getByRole("button", { name: "Create campaign", exact: true }).click();
  const created = await creation;
  expect(created.status(), await created.text()).toBe(201);
  const campaign = await created.json() as { campaign_id: string; viewed_revision: { revision_id: string; ordinal: number; tree_digest: string } };
  const revision = campaign.viewed_revision;
  const creationContext = await page.request.get(`/api/v1/campaigns/${campaign.campaign_id}/revisions/${revision.revision_id}/editor/creation-context`);
  expect(creationContext.status()).toBe(200);
  const initialEditorWorkflow = (await creationContext.json() as { editor_workflow_version: number }).editor_workflow_version;

  await page.goto(`/campaigns/${campaign.campaign_id}/records/__new__?revision=${revision.revision_id}`);
  const editor = page.locator(".editor");
  await expect(editor.getByRole("heading", { name: "Create record" })).toBeVisible();
  await editor.getByLabel("Record ID", { exact: true }).fill("npc-live-validation");
  await editor.getByLabel("Displayed name", { exact: true }).fill("Live validation record");
  await editor.getByLabel("Status", { exact: true }).selectOption("draft");
  await editor.getByLabel("summary", { exact: true }).fill("The editor keeps this value after the server rejects the proposal.\n");
  await editor.getByRole("button", { name: "Add typed connection", exact: true }).click();
  const connection = editor.locator(".editor-connection").first();
  await connection.getByRole("button", { name: /Target for connection_1: choose existing record/ }).click();
  const targetDialog = page.locator(".record-picker-dialog");
  await targetDialog.getByLabel("Search existing records").fill("campaign-main");
  await targetDialog.getByRole("button", { name: "Search", exact: true }).click();
  await targetDialog.getByRole("option", { name: /campaign-main/ }).click();
  await connection.getByLabel("Context", { exact: true }).fill("A valid target before the boundary test.");

  const hideTarget = await page.request.post(`/__test_hide_atlas_record__?campaign_id=${campaign.campaign_id}&revision_id=${revision.revision_id}&record_id=campaign-main`);
  expect(hideTarget.status()).toBe(204);

  const proposalRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/editor/records/proposals"));
  const failedProposal = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/editor/records/proposals"));
  await editor.getByRole("button", { name: "Submit create proposal", exact: true }).click();
  const request = await proposalRequest;
  const response = await failedProposal;
  expect(request.postDataJSON().candidate.connections[0].target_record_id).toBe("campaign-main");
  expect(response.status(), await response.text()).toBe(422);
  const payload = await response.json() as { error: { category: string; code: string; findings: Array<Record<string, unknown>> } };
  expect(payload.error.category).toBe("proposal_validation_failure");
  expect(payload.error.code).toBe("unknown_connection_target");
  expect(payload.error.findings).toEqual([
    expect.objectContaining({
      code: "unknown_connection_target",
      location: "connections.connection_1.target_record_id",
      message: "The relationship target does not exist in this revision.",
      recovery_action: "Choose an existing record as the target.",
      retryable: false,
    }),
  ]);
  expect(JSON.stringify(payload)).not.toContain("missing-target");

  await expect(editor.getByRole("heading", { name: "Editor error" })).toBeFocused();
  const findings = editor.getByRole("list", { name: "Validation findings" });
  await expect(findings).toContainText("connections.connection_1.target_record_id");
  await expect(findings).toContainText("The relationship target does not exist in this revision.");
  await expect(findings).toContainText("Choose an existing record as the target.");
  await expect(editor.getByLabel("Record ID", { exact: true })).toHaveValue("npc-live-validation");
  await expect(editor.getByLabel("Displayed name", { exact: true })).toHaveValue("Live validation record");
  await expect(editor.getByLabel("Status", { exact: true })).toHaveValue("draft");
  await expect(editor.getByLabel("summary", { exact: true })).toHaveValue("The editor keeps this value after the server rejects the proposal.\n");
  await expect(connection).toContainText("campaign-main");
  await expect(connection.getByLabel("Context", { exact: true })).toHaveValue("A valid target before the boundary test.");

  const campaigns = await page.request.get("/api/v1/campaigns");
  expect(campaigns.status()).toBe(200);
  const current = (await campaigns.json() as { campaigns: Array<{ campaign_id: string; head_revision: { revision_id: string } }> }).campaigns.find((item) => item.campaign_id === campaign.campaign_id);
  expect(current?.head_revision.revision_id).toBe(revision.revision_id);

  const afterFailureContext = await page.request.get(`/api/v1/campaigns/${campaign.campaign_id}/revisions/${revision.revision_id}/editor/creation-context`);
  expect(afterFailureContext.status()).toBe(200);
  expect((await afterFailureContext.json() as { editor_workflow_version: number }).editor_workflow_version).toBe(initialEditorWorkflow);

  const query = new URLSearchParams({ revision_id: revision.revision_id, revision_ordinal: String(revision.ordinal), tree_digest: revision.tree_digest, limit: "50" });
  const proposals = await page.request.get(`/api/v1/campaigns/${campaign.campaign_id}/atlas/proposals?${query}`);
  expect(proposals.status()).toBe(200);
  expect((await proposals.json() as { items: unknown[] }).items).toEqual([]);
});
