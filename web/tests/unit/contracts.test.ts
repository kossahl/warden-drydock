import type { CampaignAtlas } from "../../src/contracts/v1";
import atlasExamples from "../../../docs/contracts/hosted/http/atlas/v1/examples.json";
import atlasSchema from "../../../docs/contracts/hosted/http/atlas/v1/atlas.schema.json";
import { campaigns, detail, fullHistory, neighborhood, overview, records, workflow } from "../fixtures/atlas";

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

  it("keeps every consumed Atlas response fixture aligned with the committed closed examples", () => {
    const consumed = {
      atlas_campaign_collection: ["campaign_collection", campaigns],
      atlas_overview: ["overview", overview],
      atlas_record_library_result: ["record_library_result", records],
      atlas_record_detail: ["record_detail", detail],
      atlas_depth_1_neighborhood: ["neighborhood", neighborhood],
      atlas_approved_history_collection: ["history_collection", fullHistory],
      atlas_workflow_summary: ["workflow_summary", workflow],
    } as const;
    const examples = new Map(atlasExamples.examples.map((example) => [example.contract_name, example]));
    for (const [contractName, [definitionName, typedFixture]] of Object.entries(consumed)) {
      const definition = atlasSchema.$defs[definitionName as keyof typeof atlasSchema.$defs] as { additionalProperties?: boolean; required?: string[] };
      const committed = examples.get(contractName);
      expect(committed, `${contractName} committed example`).toBeDefined();
      expect(definition.additionalProperties).toBe(false);
      expect(Object.keys(committed!).sort()).toEqual([...definition.required!].sort());
      expect(Object.keys(typedFixture).sort()).toEqual([...definition.required!].sort());
    }
  });
});
