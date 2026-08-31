import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import atlasExamples from "../../../docs/contracts/hosted/http/atlas/v2/examples.json";
import atlasSchema from "../../../docs/contracts/hosted/http/atlas/v2/atlas.schema.json";
import { httpAtlasApi } from "../../src/api/atlasClient";
import { campaigns, detail, fullHistory, generations, neighborhood, overview, proposals, records, workflow } from "../fixtures/atlas";

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
const definitions = (atlasSchema as unknown as { $defs: Record<string, { additionalProperties?: boolean }> }).$defs;
const revision = { revision_id: "revision_two", revision_ordinal: 2, tree_digest: "a".repeat(64) };

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);
const validateAtlas = ajv.compile(atlasSchema);

const handlers: Record<string, () => Promise<unknown>> = {
  atlas_campaign_collection: () => httpAtlasApi.campaigns(),
  atlas_overview: () => httpAtlasApi.overview("campaign_atlas", revision),
  atlas_record_library_result: () => httpAtlasApi.records("campaign_atlas", { ...revision, q: "", types: [], authorities: [], statuses: [] }),
  atlas_record_detail: () => httpAtlasApi.record("campaign_atlas", "record-one", revision),
  atlas_depth_1_neighborhood: () => httpAtlasApi.neighborhood("campaign_atlas", "record-one", revision),
  atlas_approved_history_collection: () => httpAtlasApi.history("campaign_atlas", { ...revision }),
  atlas_workflow_summary: () => httpAtlasApi.workflow("campaign_atlas", revision),
  atlas_generation_collection: () => httpAtlasApi.generations("campaign_atlas", { ...revision, actions: [], statuses: [] }),
  atlas_proposal_collection: () => httpAtlasApi.proposals("campaign_atlas", { ...revision, statuses: [] }),
};

describe("hosted Atlas contract v2 parity", () => {
  it("keeps every consumed Atlas response fixture aligned with the committed closed examples", () => {
    for (const [contractName, [definitionName, typedFixture]] of Object.entries(consumed)) {
      const committed = examples.get(contractName);
      const definition = definitions[definitionName];
      expect(committed, `${contractName} committed example`).toBeDefined();
      expect(definition.additionalProperties).toBe(false);
      expect(committed, `${contractName} committed example matches the typed fixture`).toEqual(typedFixture);
      expect(validateAtlas(committed), `${contractName} committed example is a valid atlas v2 document`).toBe(true);
      expect(validateAtlas.errors, `${contractName} committed example schema errors`).toBeNull();
      expect(validateAtlas(typedFixture), `${contractName} typed fixture is a valid atlas v2 document`).toBe(true);
      expect(validateAtlas.errors, `${contractName} typed fixture schema errors`).toBeNull();
    }
  });

  it("renders a schema-valid but drifted committed example a distinguished defect", () => {
    const overview = examples.get("atlas_overview")!;
    const drifted = { ...overview, record_count: 1 };
    expect(validateAtlas(drifted)).toBe(true);
    expect(drifted).not.toEqual(consumed.atlas_overview[1]);
    const open = { ...overview, unexpected_property: true };
    expect(validateAtlas(open)).toBe(false);
  });

  it("resolves every committed example through the production Atlas HTTP client", async () => {
    let current: unknown;
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, headers: new Headers({ "Content-Type": "application/json" }), json: async () => current }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      for (const [contractName, [, typedFixture]] of Object.entries(consumed)) {
        current = examples.get(contractName);
        const parsed = await handlers[contractName]();
        expect(parsed, `${contractName} resolves through the production client`).toEqual(typedFixture);
      }
    } finally {
      vi.unstubAllGlobals();
    }
    expect(fetchMock).toHaveBeenCalledTimes(Object.keys(consumed).length);
  });
});
