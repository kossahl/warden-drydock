import { expect, test, type Locator, type Page } from "@playwright/test";
import { installAtlasApi } from "./atlas-api";

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
  await installAtlasApi(page);
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
