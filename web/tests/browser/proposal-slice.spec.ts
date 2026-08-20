import { expect, test, type Page, type Route } from "@playwright/test";

const digest = (value: string) => value.repeat(64);
const source = { source_id: "campaign-main", authority: "canon", revision_id: "revision_alpha", order: 1, excerpt: "# Synthetic Campaign", excerpt_digest: digest("a") };
const campaign = (revision = "revision_alpha") => ({ contract_name: "campaign_revision_view", contract_version: 1, campaign_id: "campaign_alpha", campaign_name: "Synthetic Campaign", adapter_id: "mothership", viewed_revision: { revision_id: revision, ordinal: revision === "revision_beta" ? 2 : 1, tree_digest: digest(revision === "revision_beta" ? "b" : "a"), validation_status: "passed" }, head_revision: revision, records: [{ record_id: "campaign-main", record_type: "campaign", name: "Synthetic Campaign", authority: "canon" }] });
const record = (revision = "revision_alpha") => ({ contract_name: "record_view", contract_version: 1, campaign_id: "campaign_alpha", revision_id: revision, record_id: "campaign-main", record_type: "campaign", name: "Synthetic Campaign", authority: "canon", content: revision === "revision_beta" ? "# Synthetic Campaign\n\n## Proposed addition\n\nThe station is quiet." : "# Synthetic Campaign" });

function proposal(status: "draft" | "conflict" | "published" = "draft", version = 1) {
  return { contract_name: "proposal_view", contract_version: 1, proposal_id: "proposal_alpha", proposal_version: version, campaign_id: "campaign_alpha", generation_id: "generation_alpha", source_revision: "revision_alpha", base_revision: "revision_alpha", source_set_digest: digest("b"), terminal_draft_digest: digest("c"), artifact_kind: "proposal", status, exact_diff: [{ change_id: "change_alpha", subject_id: "campaign-main", change_type: "update", record_type: "campaign", from_authority: "canon", to_authority: "canon", before_content: "# Synthetic Campaign", after_content: "# Synthetic Campaign\n\n## Proposed addition\n\nThe station is quiet.", before_digest: digest("a"), after_digest: digest("d") }], diff_digest: digest("e"), proposal_payload_digest: digest("f"), validation_status: "passed", published_revision_id: status === "published" ? "revision_beta" : null };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installApi(page: Page, mode: "happy" | "conflict" | "retry") {
  let consent = false;
  let generationId = "generation_alpha";
  let streamAttempts = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/provider/readiness") return fulfillJson(route, { contract_name: "provider_readiness_response", contract_version: 1, provider_configured: true, provider_available: true, consent_current: consent, consent_identity_digest: digest("9"), ai_available: consent });
    if (path === "/api/v1/provider/consent") { consent = true; return fulfillJson(route, { contract_name: "provider_readiness_response", contract_version: 1, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: digest("9"), ai_available: true }); }
    if (path === "/api/v1/campaigns") return fulfillJson(route, campaign(), 201);
    if (/\/revisions\/revision_(alpha|beta)$/.test(path)) return fulfillJson(route, campaign(path.endsWith("beta") ? "revision_beta" : "revision_alpha"));
    if (path.includes("/records/")) return fulfillJson(route, record(path.includes("revision_beta") ? "revision_beta" : "revision_alpha"));
    if (path.endsWith("/asks")) {
      generationId = (request.postDataJSON() as { generation_id: string }).generation_id;
      return fulfillJson(route, { contract_name: "generation_view", contract_version: 1, generation_id: generationId, campaign_id: "campaign_alpha", source_revision: "revision_alpha", draft_authority: "draft", status: "pending", sources: [source], source_set_digest: digest("b"), last_sequence: 0, terminal_content: null, terminal_content_digest: null }, 202);
    }
    if (path.endsWith("/events")) {
      streamAttempts += 1;
      if (mode === "retry" && streamAttempts === 1) return fulfillJson(route, { contract_name: "error_response", contract_version: 1, error: { category: "provider_retryable_failure", code: "provider_retryable_failure", stage: "ask_stream", request_id: "request_stream", retryable: true } }, 503);
      const event = { contract_name: "generation_event", contract_version: 1, generation_id: generationId, sequence: 2, event_type: "delta", draft_fragment: "The station is quiet.", retryable: null };
      return route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: `id: 2\nevent: delta\ndata: ${JSON.stringify(event)}\n\n` });
    }
    if (/\/generations\/[^/]+$/.test(path)) return fulfillJson(route, { contract_name: "generation_view", contract_version: 1, generation_id: generationId, campaign_id: "campaign_alpha", source_revision: "revision_alpha", draft_authority: "draft", status: "complete", sources: [source], source_set_digest: digest("b"), last_sequence: 3, terminal_content: "The station is quiet.", terminal_content_digest: digest("c") });
    if (path.endsWith("/proposals")) return fulfillJson(route, proposal(), 201);
    if (path.endsWith("/corrections")) return fulfillJson(route, proposal("draft", 2), 201);
    if (path.endsWith("/rejection")) return fulfillJson(route, { ...proposal(), status: "rejected" });
    if (path.endsWith("/approval")) {
      if (mode === "conflict") return fulfillJson(route, { contract_name: "proposal_approval_result", contract_version: 1, proposal: proposal("conflict"), outcome: "conflict", published_revision: null, error: { category: "stale_revision", code: "stale_campaign_head", stage: "proposal_approve", request_id: "request_alpha", retryable: false }, exact_replay: false }, 409);
      return fulfillJson(route, { contract_name: "proposal_approval_result", contract_version: 1, proposal: proposal("published"), outcome: "published", published_revision: { revision_id: "revision_beta", ordinal: 2, tree_digest: digest("b"), validation_status: "passed" }, error: null, exact_replay: false });
    }
    await route.abort();
  });
}

async function createAndAsk(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Create campaign" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic Campaign" })).toBeVisible();
  await page.getByRole("button", { name: "Allow grounded AI" }).click();
  await page.getByRole("button", { name: "Ask grounded question" }).click();
}

test("creates, grounds, inspects exact diff, approves, and opens the validated revision", async ({ page }) => {
  await createAndAsk(page);
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  await expect(page.getByText("campaign-main", { exact: true })).toBeVisible();
  await page.getByText("Inspect excerpt").click();
  await expect(page.getByRole("region", { name: "Sources" })).toContainText("id: campaign-main");
  await expect(page.getByRole("heading", { name: "Grounded Draft" })).toBeVisible();
  await expect(page.getByText("The grounded source is campaign-main.")).toBeVisible();
  const baseRevision = await page.locator(".revision-id code").innerText();
  await page.getByRole("button", { name: "Create proposal" }).click();
  await expect(page.getByRole("region", { name: "Complete before and after content" })).toContainText("## Proposed addition");
  await page.getByRole("button", { name: "Approve exact diff" }).click();
  await expect(page.locator(".revision-id code")).not.toHaveText(baseRevision);
  await expect(page.locator(".revision-id code")).toHaveText(/^revision_[a-f0-9]{20}$/);
  await expect(page.getByText("Viewed revision 2 · Head")).toBeVisible();
  await expect(page.getByText("## Proposed addition", { exact: false }).first()).toBeVisible();
});

test("preserves a proposal when approval finds a stale head", async ({ page }) => {
  await installApi(page, "conflict");
  await createAndAsk(page);
  await page.getByRole("button", { name: "Create proposal" }).click();
  await page.getByRole("button", { name: "Approve exact diff" }).click();
  await expect(page.getByRole("alert")).toContainText("proposal is preserved");
  await expect(page.getByRole("heading", { name: "Exact diff" })).toBeVisible();
  await expect(page.getByText("revision_beta")).toHaveCount(0);
});

test("announces a failed stream and resumes from its last event", async ({ page }) => {
  await installApi(page, "retry");
  await createAndAsk(page);
  await expect(page.getByRole("alert")).toContainText("provider_retryable_failure");
  await page.getByRole("button", { name: "Resume stream after event 0" }).click();
  await expect(page.getByRole("heading", { name: "Grounded Draft" })).toBeVisible();
  await expect(page.getByText("The station is quiet.")).toBeVisible();
});
