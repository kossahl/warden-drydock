import type { CampaignAtlas } from "../../src/contracts/v1";
import atlasExamples from "../../../docs/contracts/hosted/http/atlas/v1/examples.json";
import atlasSchema from "../../../docs/contracts/hosted/http/atlas/v1/atlas.schema.json";
import { campaigns, detail, fullHistory, neighborhood, overview, records, workflow } from "../fixtures/atlas";

type SchemaNode = {
  $ref?: string;
  oneOf?: SchemaNode[];
  type?: string | string[];
  const?: unknown;
  enum?: unknown[];
  pattern?: string;
  required?: string[];
  properties?: Record<string, SchemaNode>;
  additionalProperties?: boolean;
  items?: SchemaNode;
};
const schemaRoot = atlasSchema as unknown as { $defs: Record<string, SchemaNode> };

function validateSchema(value: unknown, node: SchemaNode, path = "$."): void {
  if (node.$ref) return validateSchema(value, schemaRoot.$defs[node.$ref.split("/").at(-1)!], path);
  if (node.oneOf) {
    const matches = node.oneOf.filter((candidate) => { try { validateSchema(value, candidate, path); return true; } catch { return false; } });
    if (matches.length !== 1) throw new Error(`${path} matched ${matches.length} oneOf branches`);
    return;
  }
  if (node.const !== undefined && value !== node.const) throw new Error(`${path} does not equal its const`);
  if (node.enum && !node.enum.includes(value)) throw new Error(`${path} is outside its enum`);
  const types = typeof node.type === "string" ? [node.type] : node.type;
  if (types) {
    const actual = value === null ? "null" : Array.isArray(value) ? "array" : Number.isInteger(value) ? "integer" : typeof value;
    if (!types.includes(actual) && !(actual === "integer" && types.includes("number"))) throw new Error(`${path} expected ${types.join("|")} but received ${actual}`);
  }
  if (node.pattern && typeof value === "string" && !new RegExp(node.pattern).test(value)) throw new Error(`${path} does not match ${node.pattern}`);
  if (node.properties && value !== null && typeof value === "object" && !Array.isArray(value)) {
    const object = value as Record<string, unknown>;
    for (const required of node.required ?? []) if (!(required in object)) throw new Error(`${path}${required} is required`);
    if (node.additionalProperties === false) for (const key of Object.keys(object)) if (!(key in node.properties)) throw new Error(`${path}${key} is not allowed`);
    for (const [key, child] of Object.entries(node.properties)) if (key in object) validateSchema(object[key], child, `${path}${key}.`);
  }
  if (node.items && Array.isArray(value)) value.forEach((item, index) => validateSchema(item, node.items!, `${path}${index}.`));
}

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
      const definition = schemaRoot.$defs[definitionName];
      const committed = examples.get(contractName);
      expect(committed, `${contractName} committed example`).toBeDefined();
      expect(definition.additionalProperties).toBe(false);
      expect(() => validateSchema(committed, definition)).not.toThrow();
      expect(() => validateSchema(typedFixture, definition)).not.toThrow();
    }
  });
});
