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
