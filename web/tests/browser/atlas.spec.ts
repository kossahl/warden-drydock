import { expect, test, type Locator, type Page } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";
import { generations, headRevision, neighborhood, oldRevision, proposals } from "../fixtures/atlas";

const ready = { contract_name: "provider_readiness_response" as const, contract_version: 2 as const, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: "9".repeat(64), ai_available: true };

async function installGenerationApi(page: Page, options: { uncertainStart?: boolean; interrupted?: boolean } = {}) {
  const starts: Array<Record<string, unknown>> = [];
  const started = new Map<string, Record<string, unknown>>();
  let startAttempts = 0; let streamAttempts = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && /\/campaigns\/campaign_atlas\/revisions\/[^/]+\/generations$/.test(path)) {
      const body = request.postDataJSON() as Record<string, unknown>; starts.push(body); startAttempts += 1;
      if (options.uncertainStart && startAttempts === 1) return route.abort("connectionfailed");
      const generationId = body.generation_id as string; started.set(generationId, body);
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ contract_name: "generation_view", contract_version: 2, generation_id: generationId, campaign_id: "campaign_atlas", source_revision: "revision_two", action: body.action, context: body.context, session_id: null, draft_authority: "draft", status: "pending", sources: [{ source_id: "record-one", authority: "canon", revision_id: "revision_two", order: 1, excerpt: "Pinned excerpt", excerpt_digest: "1".repeat(64) }], source_set_digest: "2".repeat(64), last_sequence: 0, terminal_content: null, terminal_content_digest: null }) });
    }
    if (request.method() === "GET" && path.endsWith("/events")) {
      streamAttempts += 1;
      if (options.interrupted && streamAttempts === 1) return route.abort("connectionfailed");
      const generationId = path.split("/").at(-2)!; const event = { contract_name: "generation_event", contract_version: 2, generation_id: generationId, sequence: 1, event_type: "delta", draft_fragment: "Browser Draft.", retryable: null };
      return route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: `id: 1\nevent: delta\ndata: ${JSON.stringify(event)}\n\n` });
    }
    if (request.method() === "GET" && /\/generations\/[^/]+$/.test(path)) {
      const generationId = path.split("/").at(-1)!; const body = started.get(generationId)!;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ contract_name: "generation_view", contract_version: 2, generation_id: generationId, campaign_id: "campaign_atlas", source_revision: "revision_two", action: body.action, context: body.context, session_id: null, draft_authority: "draft", status: "complete", sources: [{ source_id: "record-one", authority: "canon", revision_id: "revision_two", order: 1, excerpt: "Pinned excerpt", excerpt_digest: "1".repeat(64) }], source_set_digest: "2".repeat(64), last_sequence: 1, terminal_content: "Browser Draft.", terminal_content_digest: "3".repeat(64) }) });
    }
    return route.fallback();
  });
  return starts;
}

async function tabTo(page: Page, target: Locator, limit = 60) {
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((element) => element === document.activeElement)) return;
  }
  throw new Error("Keyboard focus did not reach the target");
}

test("root proposal workspace remains accessible before and after Atlas", async ({ page }) => {
  await installAtlasApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Create a campaign" })).toBeVisible();
  await page.goto("/campaigns/campaign_atlas?revision=revision_two");
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic Atlas" })).toBeVisible();
  await page.getByRole("link", { name: "Warden Drydock proposal workspace" }).click();
  await expect(page.getByRole("heading", { name: "Create a campaign" })).toBeVisible();
});

test("Back and Forward restore route, revision, search, filters, and cursor", async ({ page }) => {
  await installAtlasApi(page);
  await page.goto("/campaigns/campaign_atlas/records?revision=revision_two&q=station&type=npc&authority=canon&status=canon&cursor=page_two");
  await expect(page.getByRole("heading", { level: 1, name: "Records" })).toBeVisible();
  await page.getByRole("link", { name: "Station Keeper" }).click();
  await expect(page).toHaveURL(/\/records\/record-one\?.*cursor=page_two/);
  await page.goBack();
  await expect(page).toHaveURL(/\/records\?revision=revision_two&q=station&type=npc&authority=canon&status=canon&cursor=page_two/);
  await expect(page.getByRole("heading", { level: 1, name: "Records" })).toBeVisible();
  await page.goForward();
  await expect(page.getByRole("heading", { level: 1, name: "Station Keeper" })).toBeVisible();
});

test("Overview requests and displays only the newest five approved events", async ({ page }) => {
  await installAtlasApi(page);
  const historyRequests: string[] = [];
  page.on("request", (request) => { if (request.url().includes("/atlas/history")) historyRequests.push(request.url()); });
  await page.goto("/campaigns/campaign_atlas?revision=revision_two");
  const recent = page.getByRole("heading", { name: "Most recent approved revisions" }).locator("..");
  await expect(recent.getByRole("listitem")).toHaveCount(5);
  await expect(recent.getByRole("heading", { level: 3 }).first()).toHaveText("Revision 6");
  expect(historyRequests).toHaveLength(1);
  expect(historyRequests[0]).toContain("limit=5");
  expect(historyRequests[0]).toContain("direction=backward");
});

test("historical view stays selected until Open head", async ({ page }) => {
  await installAtlasApi(page);
  await page.goto("/campaigns/campaign_atlas?revision=revision_one");
  await expect(page.getByText(/Viewed revision 1/)).toBeVisible();
  await page.getByRole("link", { name: "Open head" }).click();
  await expect(page).toHaveURL("/campaigns/campaign_atlas?revision=revision_two");
  await expect(page.getByText(/Viewed revision 2/)).toBeVisible();
});

test("Atlas navigation and filters work by keyboard at 320 by 720 without page overflow", async ({ page }) => {
  const longName = `Legacy-${"ship".repeat(40)}`;
  const longContext = `Context-${"without-breaks".repeat(45)}`;
  await installAtlasApi(page, { neighborhood: { ...neighborhood, neighbors: neighborhood.neighbors.map((item) => ({ ...item, name: longName })), edges: neighborhood.edges.map((edge) => ({ ...edge, context: longContext })) } });
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/campaigns/campaign_atlas/records?revision=revision_two&cursor=stale_cursor");
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#atlas-content")).toBeFocused();
  await expect(page.getByRole("navigation", { name: "Campaign Atlas" })).toBeVisible();
  await tabTo(page, page.getByLabel("Type"));
  await page.keyboard.press("ArrowDown");
  await expect(page).toHaveURL(/type=npc/);
  await expect(page).not.toHaveURL(/cursor=/);
  await tabTo(page, page.getByRole("link", { name: "Station Keeper" }));
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Station Keeper" })).toBeFocused();
  await expect(page.getByRole("button", { name: "Map" })).toBeHidden();
  await expect(page.locator(".relationship-list")).toBeVisible();
  await expect(page.locator(".relationship-list > li")).toHaveCount(1);
  await expect(page.locator(".relationship-list > li").first().locator("p").first()).toHaveText(longContext);
  const relationshipWidths = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(relationshipWidths.scroll).toBeLessThanOrEqual(relationshipWidths.client);
  await page.setViewportSize({ width: 640, height: 900 });
  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  const zoomedWidths = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(zoomedWidths.scroll).toBeLessThanOrEqual(zoomedWidths.client);
  await page.evaluate(() => { document.documentElement.style.zoom = ""; });
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/campaigns/campaign_atlas/records?revision=revision_two");
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await tabTo(page, page.getByRole("link", { name: "Approved history" }));
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Approved history" })).toBeFocused();
  const widths = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
});

test("regional retry restores Overview without blanking shell navigation", async ({ page }) => {
  await installAtlasApi(page, { failOverviewOnce: true });
  await page.goto("/campaigns/campaign_atlas?revision=revision_two");
  await expect(page.getByRole("navigation", { name: "Campaign Atlas" })).toBeVisible();
  const alert = page.getByRole("alert").filter({ hasText: "Atlas read failed" });
  await expect(alert).toBeVisible();
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await tabTo(page, alert.getByRole("button", { name: "Retry" }));
  await page.keyboard.press("Enter");
  await expect(page.getByText("2", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Campaign Atlas" })).toBeVisible();
});

test("wide relationship map and ordered list preserve duplicate and self occurrences", async ({ page }) => {
  const edges = [
    { ...neighborhood.edges[0], edge_id: `edge_${"1".repeat(64)}`, occurrence_order: 1, context: "First duplicate." },
    { ...neighborhood.edges[0], edge_id: `edge_${"2".repeat(64)}`, occurrence_order: 2, context: "Second duplicate." },
    { ...neighborhood.edges[0], edge_id: `edge_${"3".repeat(64)}`, occurrence_order: 3, source_record_id: "record-one", target_record_id: "record-one", relationship: "remembers", context: "Self occurrence." },
  ];
  await installAtlasApi(page, { neighborhood: { ...neighborhood, edges, total_edges: 3 } });
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two&q=station&type=npc&authority=canon&status=canon&cursor=library_page&relationship_cursor=edge_page&generation_cursor=draft_page&proposal_cursor=proposal_page");
  const controls = page.getByRole("group", { name: "Relationship view" });
  await expect(controls.getByRole("button", { name: "Map" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".relationship-map .relationship-card")).toHaveCount(3);
  await expect(page.locator(".relationship-list")).toBeHidden();
  await controls.getByRole("button", { name: "List" }).focus();
  await page.keyboard.press("Space");
  await expect(controls.getByRole("button", { name: "List" })).toBeFocused();
  const rows = page.locator(".relationship-list > li");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("First duplicate.");
  await expect(rows.nth(1)).toContainText("Second duplicate.");
  await expect(rows.nth(2)).toContainText("Self occurrence.");
  await expect(rows.nth(0).locator("p").first()).toHaveText("First duplicate.");
  await expect(rows.nth(0)).toContainText("Related record: Legacy Ship");
  await expect(rows.nth(0)).toContainText("DirectionOutgoing");
  await expect(rows.nth(0)).toContainText("TypeKnows");
  await expect(rows.nth(0)).toContainText("StateCurrent");
  const selfTarget = rows.nth(2).getByRole("link", { name: "Station Keeper" });
  await expect(selfTarget).toHaveAttribute("href", expect.stringContaining("revision=revision_two"));
  await selfTarget.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Station Keeper" })).toBeFocused();
  await expect(page).not.toHaveURL(/relationship_cursor|generation_cursor|proposal_cursor/);
  await controls.getByRole("button", { name: "List" }).click();
  const neighbor = rows.nth(0).getByRole("link", { name: "Legacy Ship" });
  await neighbor.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Legacy Ship" })).toBeFocused();
  await expect(page).toHaveURL(/revision=revision_two/);
  await expect(page).toHaveURL(/q=station/);
  await expect(page).toHaveURL(/type=npc/);
  await expect(page).toHaveURL(/cursor=library_page/);
  await expect(page).not.toHaveURL(/relationship_cursor|generation_cursor|proposal_cursor/);
});

test("workflow panels expose publication-safe states and exact deep links", async ({ page }) => {
  const generationItems = (["pending", "complete", "failed", "cancelled"] as const).map((status, index) => ({ generation_id: `generation_${status}`, action: index % 2 ? "check" as const : "ask" as const, context: { scope: "campaign" as const }, source_revision: headRevision, source_set_digest: `${index + 1}`.repeat(64), status, retryable: status === "failed" ? true : null, created_at: `2026-08-25T0${index}:00:00Z` }));
  const proposalItems = (["draft", "rejected", "conflict", "published", "quarantined"] as const).map((status, index) => ({ proposal_id: `proposal_${status}`, proposal_version: index + 1, generation_id: `generation_${status}`, action: "generate" as const, context: { scope: "record" as const, record_id: "record-one", content_digest: "c".repeat(64) }, subject_record_id: "record-one", subject_content_digest: "c".repeat(64), source_revision: headRevision, base_revision: status === "draft" ? oldRevision : headRevision, status, validation_status: "passed" as const, published_revision_id: status === "published" ? "revision_three" : null, created_at: `2026-08-25T1${index}:00:00Z` }));
  await installAtlasApi(page, { generations: { ...generations, items: generationItems }, proposals: { ...proposals, items: proposalItems } });
  await page.goto("/campaigns/campaign_atlas?revision=revision_two");
  for (const text of ["In progress", "Draft ready, not canon", "Failed, no Draft published", "Cancelled, no Draft published", "Rejected, not published", "Conflict, not published", "Published to revision revision_three", "Quarantined, publication not confirmed", "Stale base"]) await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "Open Draft" }).first()).toHaveAttribute("href", "/?generation=generation_pending");
  await expect(page.getByRole("link", { name: "Review proposal" }).first()).toHaveAttribute("href", "/?proposal=proposal_draft&version=1");
  await expect(page.getByText(/Provider reported this failure as retryable/)).toBeVisible();
});

test("Campaign and Record Ask, Check, and Generate send explicit exact contexts", async ({ page }) => {
  await installAtlasApi(page, { readiness: ready }); const starts = await installGenerationApi(page);
  await page.goto("/campaigns/campaign_atlas?revision=revision_two");
  for (const action of ["Ask", "Check", "Generate"] as const) {
    await page.getByRole("radio", { name: action }).click();
    const label = action === "Ask" ? "Question" : action === "Check" ? "Claim to check" : "Generation brief";
    await page.getByLabel(label).fill(`${action} campaign`); await page.getByRole("button", { name: `Submit ${action}` }).click(); await expect(page.getByText("Browser Draft.")).toBeVisible();
  }
  await page.goto("/campaigns/campaign_atlas/records/record-one?revision=revision_two");
  for (const action of ["Ask", "Check", "Generate"] as const) {
    await page.getByRole("radio", { name: action }).click();
    const label = action === "Ask" ? "Question" : action === "Check" ? "Claim to check" : "Generation brief";
    await page.getByLabel(label).fill(`${action} record`); await page.getByRole("button", { name: `Submit ${action}` }).click(); await expect(page.getByText("Browser Draft.")).toBeVisible();
  }
  expect(starts.map((item) => item.action)).toEqual(["ask", "check", "generate", "ask", "check", "generate"]);
  expect(starts.slice(0, 3).every((item) => (item.context as { scope: string }).scope === "campaign")).toBe(true);
  expect(starts.slice(3).every((item) => JSON.stringify(item.context) === JSON.stringify({ scope: "record", record_id: "record-one", content_digest: "c".repeat(64) }))).toBe(true);
  expect(starts.every((item) => !("session_id" in item))).toBe(true);
});

test("consent never auto-submits and uncertain start plus interruption preserve lifecycle identity", async ({ page }) => {
  let consent = false; let generationPosts = 0;
  await installAtlasApi(page, { readiness: ready });
  await page.route("**/api/v1/provider/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/readiness")) return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...ready, consent_current: consent, ai_available: consent }) });
    if (path.endsWith("/consent")) { consent = true; return route.fulfill({ contentType: "application/json", body: JSON.stringify(ready) }); }
    return route.fallback();
  });
  const starts = await installGenerationApi(page, { uncertainStart: true, interrupted: true });
  page.on("request", (request) => { if (request.method() === "POST" && /\/revisions\/revision_two\/generations$/.test(new URL(request.url()).pathname)) generationPosts += 1; });
  await page.goto("/campaigns/campaign_atlas?revision=revision_two");
  await page.getByLabel("Question").fill("Consent-bound question");
  await page.getByRole("button", { name: "Allow grounded AI" }).click();
  await expect.poll(() => generationPosts).toBe(0);
  await page.getByRole("button", { name: "Submit Ask" }).click();
  await page.getByRole("button", { name: "Retry exact start request" }).click();
  await expect(page.getByRole("alert")).toContainText("Connection interrupted");
  await page.getByRole("button", { name: "Resume after event 0" }).click();
  await expect(page.getByText("Browser Draft.")).toBeVisible();
  expect(starts).toHaveLength(2); expect(starts[0]).toEqual(starts[1]);
});
