import { expect, test } from "@playwright/test";

test("nested route reloads through the static fallback", async ({ page }) => {
  await page.goto("/campaigns/campaign_alpha/records/campaign-main");
  await expect(page.getByRole("heading", { level: 1, name: "Create a campaign" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Create a campaign" })).toBeVisible();
});

test("campaign creation remains keyboard usable at 320 CSS pixels", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await expect(page.getByLabel("Campaign name")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create campaign" })).toBeVisible();
});

test("API paths never receive the SPA fallback", async ({ request }) => {
  for (const path of ["/api", "/api?probe=fallback", "/api/v1/campaigns", "/api/v1/not-a-route"]) {
    const response = await request.get(path, { headers: { Accept: "text/html" } });
    expect(response.status()).toBe(404);
  }
});

test("production static artifact exposes no frontend dependency tree", async ({ request }) => {
  for (const path of ["/package.json", "/package-lock.json", "/node_modules/react/package.json"]) {
    expect((await request.get(path, { headers: { Accept: "application/json" } })).status()).toBe(404);
  }
});
