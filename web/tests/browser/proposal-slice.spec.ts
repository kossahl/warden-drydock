import { expect, test, type Page, type Route } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";

const digest = (value: string) => value.repeat(64);
const source = { source_id: "campaign-main", authority: "canon", revision_id: "revision_alpha", order: 1, excerpt: "# Synthetic Campaign", excerpt_digest: digest("a") };
const campaign = (revision = "revision_alpha") => ({ contract_name: "campaign_revision_view", contract_version: 2, campaign_id: "campaign_alpha", campaign_name: "Synthetic Campaign", adapter_id: "mothership", viewed_revision: { revision_id: revision, ordinal: revision === "revision_beta" ? 2 : 1, tree_digest: digest(revision === "revision_beta" ? "b" : "a"), validation_status: "passed" }, head_revision: revision, records: [{ record_id: "campaign-main", record_type: "campaign", name: "Synthetic Campaign", authority: "canon" }] });
const record = (revision = "revision_alpha") => ({ contract_name: "record_view", contract_version: 2, campaign_id: "campaign_alpha", revision_id: revision, record_id: "campaign-main", record_type: "campaign", name: "Synthetic Campaign", authority: "canon", content: revision === "revision_beta" ? "# Synthetic Campaign\n\n## Proposed addition\n\nThe station is quiet." : "# Synthetic Campaign" });

function proposal(status: "draft" | "conflict" | "published" = "draft", version = 1) {
  return { contract_name: "proposal_view", contract_version: 2, proposal_id: "proposal_alpha", proposal_version: version, campaign_id: "campaign_alpha", generation_id: "generation_alpha", source_revision: "revision_alpha", base_revision: "revision_alpha", source_set_digest: digest("b"), terminal_draft_digest: digest("c"), artifact_kind: "proposal", status, exact_diff: [{ change_id: "change_alpha", subject_id: "campaign-main", change_type: "update", record_type: "campaign", from_authority: "canon", to_authority: "canon", before_content: "# Synthetic Campaign", after_content: "# Synthetic Campaign\n\n## Proposed addition\n\nThe station is quiet.", before_digest: digest("a"), after_digest: digest("d") }], diff_digest: digest("e"), proposal_payload_digest: digest("f"), validation_status: "passed", published_revision_id: status === "published" ? "revision_beta" : null };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installApi(page: Page, mode: "happy" | "conflict" | "retry") {
  let consent = false;
  let generationId = "generation_alpha";
  let generationAction: "ask" | "check" | "generate" = "generate";
  let generationContext: { scope: "campaign" } | { scope: "record"; record_id: string; content_digest: string } = { scope: "record", record_id: "campaign-main", content_digest: digest("a") };
  let streamAttempts = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/provider/readiness") return fulfillJson(route, { contract_name: "provider_readiness_response", contract_version: 2, provider_configured: true, provider_available: true, consent_current: consent, consent_identity_digest: digest("9"), ai_available: consent });
    if (path === "/api/v1/provider/consent") { consent = true; return fulfillJson(route, { contract_name: "provider_readiness_response", contract_version: 2, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: digest("9"), ai_available: true }); }
    if (path === "/api/v1/campaigns" && request.method() === "GET") return fulfillJson(route, { contract_name: "atlas_campaign_collection", contract_version: 2, campaigns: [{ campaign_id: "campaign_alpha", campaign_name: "Synthetic Campaign", adapter_id: "mothership", recovery_state: "ready", head_revision: { revision_id: "revision_alpha", ordinal: 1, tree_digest: digest("a") }, projected_revision: { revision_id: "revision_alpha", ordinal: 1, tree_digest: digest("a") } }] });
    if (path === "/api/v1/campaigns") return fulfillJson(route, campaign(), 201);
    if (/\/revisions\/revision_(alpha|beta)$/.test(path)) return fulfillJson(route, campaign(path.endsWith("beta") ? "revision_beta" : "revision_alpha"));
    if (path.includes("/records/")) return fulfillJson(route, record(path.includes("revision_beta") ? "revision_beta" : "revision_alpha"));
    if (request.method() === "POST" && /^\/api\/v1\/campaigns\/[^/]+\/revisions\/[^/]+\/generations$/.test(path)) {
      const start = request.postDataJSON() as { generation_id: string; action: "ask" | "check" | "generate"; context: { scope: "campaign" } | { scope: "record"; record_id: string; content_digest: string }; session_id?: string };
      expect(start.action).toBe("generate");
      expect(start.context.scope).toBe("record");
      expect(start.session_id).toBeUndefined();
      generationId = start.generation_id;
      generationAction = start.action; generationContext = start.context;
      return fulfillJson(route, { contract_name: "generation_view", contract_version: 2, generation_id: generationId, campaign_id: "campaign_alpha", source_revision: "revision_alpha", action: start.action, context: start.context, session_id: start.session_id ?? null, draft_authority: "draft", status: "pending", sources: [source], source_set_digest: digest("b"), last_sequence: 0, terminal_content: null, terminal_content_digest: null }, 202);
    }
    if (path.endsWith("/events")) {
      streamAttempts += 1;
      if (mode === "retry" && streamAttempts === 1) return fulfillJson(route, { contract_name: "error_response", contract_version: 2, error: { category: "provider_retryable_failure", code: "provider_retryable_failure", stage: "ask_stream", request_id: "request_stream", retryable: true } }, 503);
      const event = { contract_name: "generation_event", contract_version: 2, generation_id: generationId, sequence: 2, event_type: "delta", draft_fragment: "The station is quiet.", retryable: null };
      return route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: `id: 2\nevent: delta\ndata: ${JSON.stringify(event)}\n\n` });
    }
    if (/\/generations\/[^/]+$/.test(path)) return fulfillJson(route, { contract_name: "generation_view", contract_version: 2, generation_id: generationId, campaign_id: "campaign_alpha", source_revision: "revision_alpha", action: generationAction, context: generationContext, session_id: null, draft_authority: "draft", status: "complete", sources: [source], source_set_digest: digest("b"), last_sequence: 3, terminal_content: "The station is quiet.", terminal_content_digest: digest("c") });
    if (path.endsWith("/proposals")) return fulfillJson(route, proposal(), 201);
    if (path.endsWith("/corrections")) return fulfillJson(route, proposal("draft", 2), 201);
    if (path.endsWith("/rejection")) return fulfillJson(route, { ...proposal(), status: "rejected" });
    if (path.endsWith("/approval")) {
      if (mode === "conflict") return fulfillJson(route, { contract_name: "proposal_approval_result", contract_version: 2, proposal: proposal("conflict"), outcome: "conflict", published_revision: null, error: { category: "stale_revision", code: "stale_campaign_head", stage: "proposal_approve", request_id: "request_alpha", retryable: false }, exact_replay: false }, 409);
      return fulfillJson(route, { contract_name: "proposal_approval_result", contract_version: 2, proposal: proposal("published"), outcome: "published", published_revision: { revision_id: "revision_beta", ordinal: 2, tree_digest: digest("b"), validation_status: "passed" }, error: null, exact_replay: false });
    }
    await route.abort();
  });
}

async function createAndGenerate(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Create campaign" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic Campaign" })).toBeVisible();
  await page.getByRole("button", { name: "Allow grounded AI" }).click();
  await page.getByRole("radio", { name: "Generate" }).click();
  await page.getByRole("button", { name: "Submit Generate" }).click();
}

test("opens an ongoing campaign from the start page", async ({ page }) => {
  await installAtlasApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Campaigns" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Synthetic Atlas" })).toBeVisible();
  await page.getByRole("link", { name: "Open campaign" }).click();
  await expect(page).toHaveURL("/campaigns/campaign_atlas?revision=revision_two");
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic Atlas" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Browse records" })).toBeVisible();
});

test("creates, grounds, inspects exact diff, approves, and opens the validated revision", async ({ page }) => {
  await test.step("create, ground, and inspect the draft", async () => {
    await installApi(page, "happy");
    await createAndGenerate(page);
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Sources" })).toContainText("record:campaign-main");
    await expect(page.getByText("campaign-main", { exact: true })).toBeVisible();
    await page.getByText("Inspect excerpt").click();
    await expect(page.getByRole("region", { name: "Sources" })).toContainText("# Synthetic Campaign");
    await expect(page.getByRole("heading", { name: "Grounded Draft" })).toBeVisible();
    await expect(page.getByText("The station is quiet.")).toBeVisible();
  });
  const baseRevision = await page.locator(".revision-id code").innerText();
  await test.step("approve the exact diff and advance the revision", async () => {
    await page.getByRole("button", { name: "Create proposal for Synthetic Campaign" }).click();
    await expect(page.getByRole("region", { name: "Complete before and after content" })).toContainText("## Proposed addition");
    await page.getByRole("button", { name: "Approve exact diff" }).click();
    await expect(page.locator(".revision-id code")).not.toHaveText(baseRevision);
    await expect(page.locator(".revision-id code")).toHaveText("revision_beta");
    await expect(page.getByText("Viewed revision 2 · Head")).toBeVisible();
    await expect(page.getByText("Stale base, not published")).toHaveCount(0);
    await expect(page.getByText("## Proposed addition", { exact: false }).first()).toBeVisible();
  });
});

test("preserves a proposal when approval finds a stale head", async ({ page }) => {
  await installApi(page, "conflict");
  await createAndGenerate(page);
  await page.getByRole("button", { name: "Create proposal for Synthetic Campaign" }).click();
  await page.getByRole("button", { name: "Approve exact diff" }).click();
  await expect(page.getByRole("alert")).toContainText("proposal is preserved");
  await expect(page.getByRole("heading", { name: "Exact diff" })).toBeVisible();
  await expect(page.getByText("revision_beta")).toHaveCount(0);
});

test("announces a failed stream and resumes from its last event", async ({ page }) => {
  await installApi(page, "retry");
  await createAndGenerate(page);
  await expect(page.getByRole("alert")).toContainText("provider_retryable_failure");
  await page.getByRole("button", { name: "Resume stream after event 0" }).click();
  await expect(page.getByRole("heading", { name: "Grounded Draft" })).toBeVisible();
  await expect(page.getByText("The station is quiet.")).toBeVisible();
});

test("exact Draft deep links gate proposal creation by action, context, status, and current head", async ({ page }) => {
  const exactDigest = "6ae57d4640095550294f9f68b8390e483c43103bcabad485716afbea7680a6fd";
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/provider/readiness") return fulfillJson(route, { contract_name: "provider_readiness_response", contract_version: 2, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: digest("9"), ai_available: true });
    if (/\/generations\/generation_/.test(path)) {
      const id = path.split("/").at(-1)!; const action = id.includes("check") ? "check" : id.includes("ask") ? "ask" : "generate"; const campaignContext = id.includes("campaign"); const historical = id.includes("historical"); const failed = id.includes("failed");
      return fulfillJson(route, { contract_name: "generation_view", contract_version: 2, generation_id: id, campaign_id: "campaign_alpha", source_revision: historical ? "revision_alpha" : "revision_beta", action, context: campaignContext ? { scope: "campaign" } : { scope: "record", record_id: "campaign-main", content_digest: exactDigest }, session_id: null, draft_authority: "draft", status: failed ? "failed" : "complete", sources: [source], source_set_digest: digest("b"), last_sequence: 3, terminal_content: "Deep-linked Draft.", terminal_content_digest: digest("c") });
    }
    if (/\/revisions\/revision_(alpha|beta)$/.test(path)) { const historical = path.endsWith("alpha"); const value = campaign(historical ? "revision_alpha" : "revision_beta"); return fulfillJson(route, { ...value, head_revision: "revision_beta" }); }
    if (path.includes("/records/campaign-main")) return fulfillJson(route, { ...record("revision_alpha"), revision_id: path.includes("revision_beta") ? "revision_beta" : "revision_alpha" });
    return route.abort();
  });
  for (const id of ["generation_ask", "generation_check", "generation_campaign", "generation_failed"]) {
    await page.goto(`/?generation=${id}`); await expect(page.getByText("Deep-linked Draft.")).toBeVisible(); await expect(page.getByRole("button", { name: /Create proposal for/ })).toHaveCount(0);
  }
  await page.goto("/?generation=generation_record_head"); await expect(page.getByRole("button", { name: "Create proposal for Synthetic Campaign" })).toBeVisible();
  await page.goto("/?generation=generation_historical"); await expect(page.getByRole("link", { name: "Open head to create a proposal." })).toBeVisible(); await expect(page.getByRole("button", { name: /Create proposal for/ })).toHaveCount(0);
});

test("a stale proposal deep link is preserved but cannot be approved", async ({ page }) => {
  const exactDigest = "6ae57d4640095550294f9f68b8390e483c43103bcabad485716afbea7680a6fd";
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/provider/readiness") return fulfillJson(route, { contract_name: "provider_readiness_response", contract_version: 2, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: digest("9"), ai_available: true });
    if (path === "/api/v1/proposals/proposal_alpha/versions/1") return fulfillJson(route, proposal());
    if (path === "/api/v1/generations/generation_alpha") return fulfillJson(route, { contract_name: "generation_view", contract_version: 2, generation_id: "generation_alpha", campaign_id: "campaign_alpha", source_revision: "revision_alpha", action: "generate", context: { scope: "record", record_id: "campaign-main", content_digest: exactDigest }, session_id: null, draft_authority: "draft", status: "complete", sources: [source], source_set_digest: digest("b"), last_sequence: 3, terminal_content: "Deep-linked Draft.", terminal_content_digest: digest("c") });
    if (path.endsWith("/revisions/revision_alpha")) return fulfillJson(route, { ...campaign("revision_alpha"), head_revision: "revision_beta" });
    if (path.endsWith("/records/campaign-main")) return fulfillJson(route, record("revision_alpha"));
    return route.abort();
  });
  await page.goto("/?proposal=proposal_alpha&version=1");
  await expect(page.getByRole("heading", { name: "Proposal version 1" })).toBeVisible();
  await expect(page.getByText("Stale base, not published")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve exact diff" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reject proposal" })).toBeVisible();
});
