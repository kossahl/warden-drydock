import atlasExamples from "../../../docs/contracts/hosted/http/atlas/v2/examples.json";
import atlasSchema from "../../../docs/contracts/hosted/http/atlas/v2/atlas.schema.json";
import { campaigns, detail, fullHistory, generations, neighborhood, overview, proposals, records, workflow } from "../fixtures/atlas";

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

describe("hosted Atlas contract v2 parity", () => {
  it("keeps every consumed Atlas response fixture aligned with the committed closed examples", () => {
    const consumed = {
      atlas_campaign_collection: ["campaign_collection", campaigns],
      atlas_overview: ["overview", overview],
      atlas_record_library_result: ["record_library_result", records],
      atlas_record_detail: ["record_detail", detail],
      atlas_depth_1_neighborhood: ["neighborhood", neighborhood],
      atlas_approved_history_collection: ["history_collection", fullHistory],
      atlas_workflow_summary: ["workflow_summary", workflow],
      atlas_generation_collection: ["generation_collection", generations],
      atlas_proposal_collection: ["proposal_collection", proposals],
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
