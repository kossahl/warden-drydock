import { expect, test } from "@playwright/test";
import type { EditorProposal, EditorRecordView } from "../../src/editor/editorClient";

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
    const pending = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/approval"));
    await page.getByRole("button", { name: "Approve and publish", exact: true }).click();
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
  await editor.getByRole("button", { name: "Add content section", exact: true }).click();
  await editor.getByLabel("section-2", { exact: true }).fill("Original second section.\n");
  for (let index = 0; index < 2; index += 1) {
    await editor.getByRole("button", { name: "Add typed connection", exact: true }).click();
    const connection = editor.locator(".editor-connection").nth(index);
    await connection.getByLabel(/^Target for/).fill("npc-target");
    await connection.getByLabel("Context", { exact: true }).fill(`Source reference ${index + 1}.`);
  }
  await submit("Submit create proposal");
  await approve();
  const originalRevision = revision;
  const original = await read("npc-source");
  expect(original.record.connections).toHaveLength(2);

  await page.goto(recordUrl("npc-source"));
  await editor.getByLabel("summary", { exact: true }).fill("First\nsecond\nthird\nfourth\nfifth\nsixth\n");
  await editor.getByLabel("section-2", { exact: true }).fill("Replacement second section.\n");
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
  await editor.getByRole("button", { name: "Load removal impact", exact: true }).click();
  const resolutions = editor.getByLabel(/^Resolution for/);
  await expect(resolutions).toHaveCount(2);
  for (const resolution of await resolutions.all()) await resolution.selectOption("remove_reference");
  const removal = await submit("Submit removal proposal", "/removal-proposals");
  expect(removal.diff.cards.filter((card) => card.kind === "reference_resolution")).toHaveLength(2);
  expect(removal.record_bindings.map((binding) => binding.record_id).sort()).toEqual(["npc-source", "npc-target"]);
  for (const resolution of await resolutions.all()) await expect(resolution).toBeDisabled();
  await editor.getByRole("button", { name: "Create correction/rebase", exact: true }).click();
  await expect(resolutions.first()).toBeEnabled();
  await resolutions.first().selectOption("redirect");
  await editor.getByRole("button", { name: "Cancel correction", exact: true }).click();
  for (const resolution of await resolutions.all()) {
    await expect(resolution).toHaveValue("remove_reference");
    await expect(resolution).toBeDisabled();
  }
  await approve();
  expect((await read("npc-source")).record.connections).toEqual([]);
  expect((await read("npc-source", originalRevision)).record.connections).toHaveLength(2);
});
