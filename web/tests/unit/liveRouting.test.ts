import { parseAtlasRoute } from "../../src/atlas/routing";

describe("Live cockpit routes", () => {
  it("keeps the pinned revision and session navigation identity on the live route", () => {
    expect(parseAtlasRoute("/campaigns/campaign_alpha/live?revision=revision_12&session=session_alpha")).toMatchObject({
      kind: "live", campaignId: "campaign_alpha", revisionId: "revision_12", sessionId: "session_alpha",
    });
  });
});
