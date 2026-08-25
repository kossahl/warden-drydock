import type { Page, Route } from "@playwright/test";
import { campaigns, detail, fullHistory, headRevision, neighborhood, newestFiveHistory, oldRevision, overview, readinessUnavailable, recordHistory, records, workflow } from "../fixtures/atlas";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function installAtlasApi(page: Page, options: { failOverviewOnce?: boolean } = {}) {
  let overviewAttempts = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() !== "GET") return route.fallback();
    if (path === "/api/v1/provider/readiness") return json(route, readinessUnavailable);
    if (path === "/api/v1/campaigns") return json(route, campaigns);
    if (/\/revisions\/revision_(one|two)$/.test(path)) return json(route, { contract_name: "campaign_revision_view", contract_version: 2, campaign_id: "campaign_atlas", campaign_name: "Synthetic Atlas", adapter_id: "mothership", viewed_revision: path.endsWith("one") ? oldRevision : headRevision, head_revision: headRevision.revision_id, records: [] });
    if (path.endsWith("/atlas/overview")) {
      overviewAttempts += 1;
      if (options.failOverviewOnce && overviewAttempts === 1) return json(route, { error: { code: "service_unavailable" } }, 503);
      return json(route, overview);
    }
    if (path.endsWith("/atlas/workflow-summary")) return json(route, workflow);
    if (path.endsWith("/neighborhood")) return json(route, neighborhood);
    if (/\/atlas\/records\/[^/]+$/.test(path)) return json(route, detail);
    if (path.endsWith("/atlas/records")) return json(route, records);
    if (path.endsWith("/atlas/history")) {
      if (url.searchParams.get("limit") === "5") return json(route, newestFiveHistory);
      if (url.searchParams.has("subject_record_id")) return json(route, recordHistory);
      return json(route, fullHistory);
    }
    return route.abort();
  });
}
