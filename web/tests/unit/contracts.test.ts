import type { CampaignAtlas } from "../../src/contracts/v1";

const atlasExample = {
  contract_name: "campaign_atlas",
  contract_version: 1,
  campaign: { campaign_id: "campaign_alpha", name: "Synthetic Campaign", adapter_id: "mothership" },
  viewed_revision: { revision_id: "revision_12", ordinal: 12, tree_digest: "a".repeat(64) },
  head_revision: "revision_12",
  entities: [{ entity_id: "entity_station", entity_type: "location", name: "Synthetic Station", authority: "canon" }],
  connections: [{
    connection_id: "connection_one",
    from_entity_id: "entity_station",
    to_entity_id: "entity_ship",
    label: "signals",
    direction: "directed",
  }],
  backlinks: [{
    source_entity_id: "entity_station",
    target_entity_id: "entity_ship",
    connection_id: "connection_one",
  }],
  history: [{ event_id: "event_one", revision_id: "revision_12", authority: "canon", title: "Synthetic event" }],
  comparison: {
    from_revision: "revision_11",
    to_revision: "revision_12",
    changes: [{ subject_id: "entity_station", change_type: "changed" }],
  },
} as const satisfies CampaignAtlas;

describe("hosted contract v1 parity", () => {
  it("represents every required Atlas view-model family", () => {
    expect(atlasExample.backlinks).toHaveLength(1);
    expect(atlasExample.history).toHaveLength(1);
    expect(atlasExample.comparison.changes).toHaveLength(1);
  });
});
