import { expect, test } from "@playwright/test";

test("nested campaign route reloads through the static fallback", async ({ page }) => {
  await page.goto("/campaigns/campaign_alpha/atlas");
  await expect(page.getByRole("heading", { level: 1, name: "Campaign Atlas" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Campaign Atlas" })).toBeVisible();
});

test("shell remains usable at 320 CSS pixels", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/campaigns/campaign_alpha/atlas");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
  await expect(page.getByText("Viewed revision 12 · Head")).toBeVisible();
});

test("API paths never receive the SPA fallback", async ({ request }) => {
  for (const path of ["/api", "/api?probe=fallback", "/api/v1/campaigns"]) {
    const response = await request.get(path, { headers: { Accept: "text/html" } });
    expect(response.status()).toBe(404);
  }
});

test("production static artifact exposes no frontend dependency tree", async ({ request }) => {
  for (const path of ["/package.json", "/package-lock.json", "/node_modules/react/package.json"]) {
    expect((await request.get(path, { headers: { Accept: "application/json" } })).status()).toBe(404);
  }
});
