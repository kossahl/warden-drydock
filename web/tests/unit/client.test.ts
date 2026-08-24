import { atlasHistoryUrl, atlasOverviewUrl, atlasRecordsUrl } from "../../src/api/atlasClient";
import { ContractClient, frontendCapabilities, type ContractTransport } from "../../src/api/client";
import type { ContractEnvelope, OperationRequest } from "../../src/contracts/v1";

describe("typed contract client authority boundary", () => {
  it("contains only read and observation capabilities", () => {
    expect(frontendCapabilities).toEqual([
      "provider_readiness", "campaign_list", "campaign_read", "atlas_read",
      "retrieval_preview", "draft_read", "live_observe", "proposal_read", "revision_read",
    ]);
    const forbidden = ["filesystem", "database", "shell", "provider_invoke", "approve", "apply", "promote"];
    expect(frontendCapabilities.join(" ")).not.toMatch(new RegExp(forbidden.join("|")));
  });

  it("fails closed for an unknown contract version before transport", async () => {
    let called = false;
    const transport: ContractTransport = {
      async send<Response extends ContractEnvelope<string>>(): Promise<Response> {
        called = true;
        throw new Error("unexpected transport call");
      },
      async submit<Response extends ContractEnvelope<string>>(_request: OperationRequest): Promise<Response> {
        called = true;
        throw new Error("unexpected transport call");
      },
    };
    const client = new ContractClient(transport);
    expect(() => client.read("atlas_read", { contract_name: "campaign_atlas", contract_version: 2 as 1 })).toThrow(
      "unsupported_contract_version",
    );
    expect(called).toBe(false);
  });

  it("submits only a versioned API operation intent through the application service", async () => {
    const submit = vi.fn(async () => ({ contract_name: "operation_result", contract_version: 1 as const }));
    const transport: ContractTransport = {
      async send<Response extends ContractEnvelope<string>>(): Promise<Response> {
        throw new Error("unexpected read");
      },
      submit: submit as ContractTransport["submit"],
    };
    const request: OperationRequest = {
      contract_name: "operation_request",
      contract_version: 1,
      request_id: "request_alpha",
      operation: "proposal_approve",
      idempotency_key: "idem_alpha",
      payload_digest: "a".repeat(64),
      expected_revision: "revision_12",
      expected_workflow_version: 3,
      subject_id: "proposal_alpha",
      intent_digest: "b".repeat(64),
    };
    await new ContractClient(transport).submit(request);
    expect(submit).toHaveBeenCalledWith(request, undefined);
    expect(JSON.stringify(request)).not.toMatch(/filesystem|database|shell|provider_invoke|promote/);
  });
});

describe("Campaign Atlas GET serialization", () => {
  const revision = { revision_id: "revision two", revision_ordinal: 12, tree_digest: "a".repeat(64) };

  it("serializes the complete revision binding in canonical order", () => {
    expect(atlasOverviewUrl("campaign/atlas", revision)).toBe(`/campaigns/campaign%2Fatlas/atlas/overview?revision_id=revision+two&revision_ordinal=12&tree_digest=${"a".repeat(64)}`);
  });

  it("uses repeated encoded filter keys, limit 50, and the supplied cursor", () => {
    const url = atlasRecordsUrl("campaign_atlas", { ...revision, q: "station & ship", types: ["npc", "capital ship"], authorities: ["canon", "preparation"], statuses: ["canon", "accepted"], cursor: "next/+=" });
    const parsed = new URL(url, "http://drydock.local");
    expect(parsed.searchParams.getAll("type")).toEqual(["npc", "capital ship"]);
    expect(parsed.searchParams.getAll("authority")).toEqual(["canon", "preparation"]);
    expect(parsed.searchParams.getAll("status")).toEqual(["canon", "accepted"]);
    expect(parsed.searchParams.get("q")).toBe("station & ship");
    expect(parsed.searchParams.get("limit")).toBe("50");
    expect(parsed.searchParams.get("cursor")).toBe("next/+=");
    expect(url).toContain("q=station+%26+ship");
    expect(url).toContain("type=capital+ship");
    expect(url).toContain("cursor=next%2F%2B%3D");
    expect(url).not.toContain(",");
  });

  it("serializes newest-first history without crawling pages", () => {
    const url = atlasHistoryUrl("campaign_atlas", { ...revision, subject_record_id: "record/one", limit: 5, cursor: "older page", direction: "backward" });
    expect(url).toBe(`/campaigns/campaign_atlas/atlas/history?revision_id=revision+two&revision_ordinal=12&tree_digest=${"a".repeat(64)}&subject_record_id=record%2Fone&limit=5&cursor=older+page&direction=backward`);
  });
});
