import type { ContractEnvelope, OperationRequest } from "../contracts/v1";

export const frontendCapabilities = [
  "provider_readiness",
  "campaign_list",
  "campaign_read",
  "atlas_read",
  "retrieval_preview",
  "draft_read",
  "live_observe",
  "proposal_read",
  "revision_read",
] as const;

export type FrontendCapability = (typeof frontendCapabilities)[number];

export interface ContractTransport {
  send<Response extends ContractEnvelope<string>>(
    capability: FrontendCapability,
    request: ContractEnvelope<string>,
    signal?: AbortSignal,
  ): Promise<Response>;
  submit<Response extends ContractEnvelope<string>>(
    request: OperationRequest,
    signal?: AbortSignal,
  ): Promise<Response>;
}

export class ContractClient {
  public constructor(private readonly transport: ContractTransport) {}

  public read<Response extends ContractEnvelope<string>>(
    capability: FrontendCapability,
    request: ContractEnvelope<string>,
    signal?: AbortSignal,
  ): Promise<Response> {
    if (request.contract_version !== 1) {
      throw new Error("unsupported_contract_version");
    }
    return this.transport.send<Response>(capability, request, signal);
  }

  public submit<Response extends ContractEnvelope<string>>(
    request: OperationRequest,
    signal?: AbortSignal,
  ): Promise<Response> {
    if (request.contract_version !== 1) {
      throw new Error("unsupported_contract_version");
    }
    return this.transport.submit<Response>(request, signal);
  }
}
