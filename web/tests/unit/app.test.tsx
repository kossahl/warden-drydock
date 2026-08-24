import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../../src/App";
import type { SliceApi } from "../../src/api/client";
import type { CampaignRevisionView, GenerationEvent, GenerationView, ProposalApprovalResult, ProposalView, ProviderReadiness, RecordView } from "../../src/contracts/v1";

const hex = (value: string) => value.repeat(64);
const readiness: ProviderReadiness = { contract_name: "provider_readiness_response", contract_version: 1, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: hex("9"), ai_available: true };
const campaign: CampaignRevisionView = { contract_name: "campaign_revision_view", contract_version: 1, campaign_id: "campaign_alpha", campaign_name: "Synthetic Campaign", adapter_id: "mothership", viewed_revision: { revision_id: "revision_alpha", ordinal: 1, tree_digest: hex("a"), validation_status: "passed" }, head_revision: "revision_alpha", records: [{ record_id: "campaign-main", record_type: "campaign", name: "Synthetic Campaign", authority: "preparation" }] };
const record: RecordView = { contract_name: "record_view", contract_version: 1, campaign_id: "campaign_alpha", revision_id: "revision_alpha", record_id: "campaign-main", record_type: "campaign", name: "Synthetic Campaign", authority: "preparation", content: "# Synthetic Campaign" };
const pending: GenerationView = { contract_name: "generation_view", contract_version: 1, generation_id: "generation_alpha", campaign_id: "campaign_alpha", source_revision: "revision_alpha", draft_authority: "draft", status: "pending", sources: [{ source_id: "campaign-main", authority: "preparation", revision_id: "revision_alpha", order: 1, excerpt: "# Synthetic Campaign", excerpt_digest: hex("a") }], source_set_digest: hex("b"), last_sequence: 0, terminal_content: null, terminal_content_digest: null };
const complete: GenerationView = { ...pending, status: "complete", last_sequence: 3, terminal_content: "The station is quiet.", terminal_content_digest: hex("c") };
const proposal: ProposalView = { contract_name: "proposal_view", contract_version: 1, proposal_id: "proposal_alpha", proposal_version: 1, campaign_id: "campaign_alpha", generation_id: "generation_alpha", source_revision: "revision_alpha", base_revision: "revision_alpha", source_set_digest: hex("b"), terminal_draft_digest: hex("c"), artifact_kind: "proposal", status: "draft", exact_diff: [{ change_id: "change_alpha", subject_id: "campaign-main", change_type: "update", record_type: "campaign", from_authority: "preparation", to_authority: "preparation", before_content: "# Synthetic Campaign", after_content: "# Synthetic Campaign\n\n## Proposed addition\n\nThe station is quiet.", before_digest: hex("a"), after_digest: hex("d") }], diff_digest: hex("e"), proposal_payload_digest: hex("f"), validation_status: "passed", published_revision_id: null };

function fakeApi(overrides: Partial<SliceApi> = {}): SliceApi {
  return {
    readiness: vi.fn(async () => readiness), consent: vi.fn(async () => readiness), createCampaign: vi.fn(async () => campaign), readRevision: vi.fn(async () => campaign), readRecord: vi.fn(async () => record), startAsk: vi.fn(async () => pending), resumeAsk: vi.fn(async (): Promise<GenerationEvent[]> => [{ contract_name: "generation_event", contract_version: 1, generation_id: "generation_alpha", sequence: 2, event_type: "delta", draft_fragment: "The station is quiet.", retryable: null }]), readGeneration: vi.fn(async () => complete), createProposal: vi.fn(async () => proposal), correctProposal: vi.fn(async () => ({ ...proposal, proposal_version: 2 })), rejectProposal: vi.fn(async (): Promise<ProposalView> => ({ ...proposal, status: "rejected" })), approveProposal: vi.fn(async (): Promise<ProposalApprovalResult> => ({ contract_name: "proposal_approval_result", contract_version: 1, proposal: { ...proposal, status: "published", published_revision_id: "revision_beta" }, outcome: "published", published_revision: { revision_id: "revision_beta", ordinal: 2, tree_digest: hex("1"), validation_status: "passed" }, error: null, exact_replay: false })), ...overrides,
  };
}

async function openRecord(api: SliceApi) {
  render(<App api={api} />);
  await screen.findByText("Provider: Ready");
  fireEvent.click(screen.getByRole("button", { name: "Create campaign" }));
  await screen.findByRole("heading", { level: 1, name: "Synthetic Campaign" });
}

async function createDraftAndProposal(api: SliceApi) {
  await openRecord(api);
  fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
  await screen.findByText("The station is quiet.");
  fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));
  await screen.findByRole("heading", { name: "Exact diff" });
}

describe("proposal browser slice", () => {
  it("distinguishes provider setup, consent, and ready states", async () => {
    const setupRequired: ProviderReadiness = { ...readiness, provider_configured: false, provider_available: false, consent_current: false, consent_identity_digest: null, ai_available: false };
    const setupApi = fakeApi({ readiness: vi.fn(async () => setupRequired) });
    render(<App api={setupApi} />);
    await screen.findByText("Provider: Setup required");
    fireEvent.click(screen.getByRole("button", { name: "Create campaign" }));
    await screen.findByRole("heading", { level: 1, name: "Synthetic Campaign" });
    expect(screen.queryByRole("button", { name: "Allow grounded AI" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask grounded question" })).toBeDisabled();

    cleanup();
    const consentRequired: ProviderReadiness = { ...readiness, consent_current: false, ai_available: false };
    render(<App api={fakeApi({ readiness: vi.fn(async () => consentRequired) })} />);
    await screen.findByText("Provider: Consent required");

    cleanup();
    const unavailable: ProviderReadiness = { ...readiness, provider_available: false, consent_current: false, ai_available: false };
    render(<App api={fakeApi({ readiness: vi.fn(async () => unavailable) })} />);
    await screen.findByText("Provider: Unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Create campaign" }));
    await screen.findByRole("heading", { level: 1, name: "Synthetic Campaign" });
    expect(screen.queryByRole("button", { name: "Allow grounded AI" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask grounded question" })).toBeDisabled();

    cleanup();
    render(<App api={fakeApi()} />);
    await screen.findByText("Provider: Ready");
  });

  it("shows explicit consent and disables campaign creation while it is in flight", async () => {
    let finishCreate!: (value: CampaignRevisionView) => void;
    const notConsented = { ...readiness, consent_current: false, ai_available: false };
    const api = fakeApi({ readiness: vi.fn(async () => notConsented), createCampaign: vi.fn(() => new Promise<CampaignRevisionView>((resolve) => { finishCreate = resolve; })) });
    render(<App api={api} />);
    await screen.findByText("Provider: Consent required");
    fireEvent.click(screen.getByRole("button", { name: "Create campaign" }));
    expect(screen.getByRole("button", { name: "Creating…" })).toBeDisabled();
    finishCreate(campaign);
    await screen.findByRole("button", { name: "Allow grounded AI" });
    expect(screen.getByRole("button", { name: "Ask grounded question" })).toBeDisabled();
  });

  it("creates a campaign, identifies sources, and keeps Draft and proposal separate from canon", async () => {
    await createDraftAndProposal(fakeApi());
    expect(screen.getByRole("heading", { name: "Revision record" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Grounded Draft" })).toBeInTheDocument();
    expect(screen.getByText("This text is not canon.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sources" })).toBeInTheDocument();
    expect(screen.getAllByText("revision_alpha").length).toBeGreaterThan(1);
    expect(screen.getByRole("heading", { name: "Proposal version 1" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Complete before and after content" })).toHaveTextContent("Before · preparation");
    expect(screen.getByRole("heading", { name: "Ask about this revision" })).toBeInTheDocument();
    expect(screen.queryByText("Canon question")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Complete before and after content" })).toHaveTextContent("## Proposed addition");
  });

  it("shows failed streaming and resumes after the last persisted event", async () => {
    let attempts = 0;
    const api = fakeApi({ resumeAsk: vi.fn(async () => { attempts += 1; if (attempts === 1) throw new Error("provider_retryable_failure"); return []; }) });
    await openRecord(api);
    fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Resume stream after event 0" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Resume stream after event 0" }));
    await waitFor(() => expect(api.readGeneration).toHaveBeenCalled());
  });

  it("keeps an empty pending stream resumable", async () => {
    const api = fakeApi({ resumeAsk: vi.fn(async () => []), readGeneration: vi.fn(async () => pending) });
    await openRecord(api);
    fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
    expect(await screen.findByRole("button", { name: "Resume stream after event 0" })).toBeEnabled();
    expect(screen.getByText("Stream pending. Last event 0.")).toBeInTheDocument();
  });

  it("does not advertise resume for a terminal provider failure", async () => {
    const failed = { ...pending, status: "failed" as const };
    const api = fakeApi({ resumeAsk: vi.fn(async () => []), readGeneration: vi.fn(async () => failed) });
    await openRecord(api);
    fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
    await screen.findByText("The generation failed. Submit a new question to retry.");
    expect(screen.queryByRole("button", { name: /Resume stream/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask grounded question" })).toBeEnabled();
  });

  it("resumes after the last event observed by the browser", async () => {
    let resumeCalls = 0;
    const resume = vi.fn(async (_generationId: string, _after: number): Promise<GenerationEvent[]> => {
      resumeCalls += 1;
      return resumeCalls === 1
        ? [{ contract_name: "generation_event", contract_version: 1, generation_id: "generation_alpha", sequence: 2, event_type: "delta", draft_fragment: "First. ", retryable: null }]
        : [{ contract_name: "generation_event", contract_version: 1, generation_id: "generation_alpha", sequence: 3, event_type: "delta", draft_fragment: "Third.", retryable: null }];
    });
    let reads = 0;
    const api = fakeApi({
      resumeAsk: resume,
      readGeneration: vi.fn(async () => { reads += 1; return reads === 1 ? { ...pending, last_sequence: 3 } : { ...complete, terminal_content: "First. Third." }; }),
    });
    await openRecord(api);
    fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
    const button = await screen.findByRole("button", { name: "Resume stream after event 2" });
    fireEvent.click(button);
    await screen.findByText(/First\. Third\./);
    expect(resume.mock.calls[1][1]).toBe(2);
  });

  it("reuses server-bound campaign, generation, and proposal IDs after transport loss", async () => {
    let campaignAttempts = 0;
    const createCampaign = vi.fn(async () => { campaignAttempts += 1; if (campaignAttempts === 1) throw new Error("transport_lost"); return campaign; });
    let askAttempts = 0;
    const startAsk = vi.fn(async (_campaignId: string, _revisionId: string, _prompt: string, _generationId: string) => { askAttempts += 1; if (askAttempts === 1) throw new Error("transport_lost"); return pending; });
    let proposalAttempts = 0;
    const createProposal = vi.fn(async () => { proposalAttempts += 1; if (proposalAttempts === 1) throw new Error("transport_lost"); return proposal; });
    const api = fakeApi({ createCampaign, startAsk, createProposal });
    render(<App api={api} />);
    await screen.findByText("Provider: Ready");
    fireEvent.click(screen.getByRole("button", { name: "Create campaign" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Create campaign" }));
    await screen.findByRole("heading", { level: 1, name: "Synthetic Campaign" });
    expect(createCampaign.mock.calls[0].slice(1)).toEqual(createCampaign.mock.calls[1].slice(1));

    fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Ask grounded question" }));
    await screen.findByText("The station is quiet.");
    expect(startAsk.mock.calls[0][3]).toBe(startAsk.mock.calls[1][3]);

    fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));
    await screen.findByRole("heading", { name: "Exact diff" });
    expect(createProposal.mock.calls[0].slice(2)).toEqual(createProposal.mock.calls[1].slice(2));
  });

  it("preserves a conflicting proposal and never implies publication", async () => {
    const conflict = { ...proposal, status: "conflict" as const };
    const api = fakeApi({ approveProposal: vi.fn(async (): Promise<ProposalApprovalResult> => ({ contract_name: "proposal_approval_result", contract_version: 1, proposal: conflict, outcome: "conflict", published_revision: null, error: { category: "stale_revision", code: "stale_campaign_head", stage: "proposal_approve", request_id: "request_alpha", retryable: false }, exact_replay: false })) });
    await createDraftAndProposal(api);
    fireEvent.click(screen.getByRole("button", { name: "Approve exact diff" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("proposal is preserved");
    expect(screen.getByRole("button", { name: "Create corrected version" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Approve exact diff" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Published and opened/)).not.toBeInTheDocument();
  });

  it("supports correction, rejection, and opens the validated revision after approval", async () => {
    const publishedCampaign = { ...campaign, viewed_revision: { revision_id: "revision_beta", ordinal: 2, tree_digest: hex("1"), validation_status: "passed" as const }, head_revision: "revision_beta" };
    const publishedRecord = { ...record, revision_id: "revision_beta", content: proposal.exact_diff[0].after_content };
    const api = fakeApi({ readRevision: vi.fn(async () => publishedCampaign), readRecord: vi.fn(async (_campaign, revision) => revision === "revision_beta" ? publishedRecord : record) });
    await createDraftAndProposal(api);
    fireEvent.click(screen.getByRole("button", { name: "Create corrected version" }));
    await screen.findByRole("heading", { name: "Proposal version 2" });
    fireEvent.click(screen.getByRole("button", { name: "Approve exact diff" }));
    await screen.findByText("revision_beta");
    expect(api.approveProposal).toHaveBeenCalledTimes(1);
  });

  it("supports explicit rejection without changing the record", async () => {
    const api = fakeApi();
    await createDraftAndProposal(api);
    fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));
    await waitFor(() => expect(screen.getByText("rejected")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Revision record" })).toBeInTheDocument();
  });

  it("reuses the approval idempotency key after a transport failure", async () => {
    let attempts = 0;
    const approve = vi.fn(async (_proposal: ProposalView, _head: string, _retryKey: string): Promise<ProposalApprovalResult> => {
      attempts += 1;
      if (attempts === 1) throw new Error("service_unavailable");
      return { contract_name: "proposal_approval_result", contract_version: 1, proposal: { ...proposal, status: "published", published_revision_id: "revision_beta" }, outcome: "published", published_revision: { revision_id: "revision_beta", ordinal: 2, tree_digest: hex("1"), validation_status: "passed" }, error: null, exact_replay: true };
    });
    const publishedCampaign = { ...campaign, viewed_revision: { revision_id: "revision_beta", ordinal: 2, tree_digest: hex("1"), validation_status: "passed" as const }, head_revision: "revision_beta" };
    const api = fakeApi({ approveProposal: approve, readRevision: vi.fn(async () => publishedCampaign), readRecord: vi.fn(async (_campaign, revision) => revision === "revision_beta" ? { ...record, revision_id: "revision_beta" } : record) });
    await createDraftAndProposal(api);
    fireEvent.click(screen.getByRole("button", { name: "Approve exact diff" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Approve exact diff" }));
    await waitFor(() => expect(approve).toHaveBeenCalledTimes(2));
    expect(approve.mock.calls[0][2]).toBe(approve.mock.calls[1][2]);
  });

  it("reuses correction and rejection idempotency keys after transport failures", async () => {
    let correctionAttempts = 0;
    const correct = vi.fn(async (value: ProposalView, _afterContent: string, _retryKey: string) => { correctionAttempts += 1; if (correctionAttempts === 1) throw new Error("transport_lost"); return { ...value, proposal_version: 2 }; });
    const api = fakeApi({ correctProposal: correct });
    await createDraftAndProposal(api);
    fireEvent.click(screen.getByRole("button", { name: "Create corrected version" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Create corrected version" }));
    await screen.findByRole("heading", { name: "Proposal version 2" });
    expect(correct.mock.calls[0][2]).toBe(correct.mock.calls[1][2]);

    cleanup();
    let rejectAttempts = 0;
    const reject = vi.fn(async (value: ProposalView, _retryKey: string) => { rejectAttempts += 1; if (rejectAttempts === 1) throw new Error("transport_lost"); return { ...value, status: "rejected" as const }; });
    const rejectApi = fakeApi({ rejectProposal: reject });
    await createDraftAndProposal(rejectApi);
    const rejectButtons = screen.getAllByRole("button", { name: "Reject proposal" });
    fireEvent.click(rejectButtons.at(-1)!);
    await waitFor(() => expect(reject).toHaveBeenCalledTimes(1));
    fireEvent.click(rejectButtons.at(-1)!);
    await waitFor(() => expect(reject).toHaveBeenCalledTimes(2));
    expect(reject.mock.calls[0][1]).toBe(reject.mock.calls[1][1]);
  });

  it("does not advertise deferred product capabilities", async () => {
    render(<App api={fakeApi()} />);
    await screen.findByText("Provider: Ready");
    for (const label of ["Import", "Export", "Player", "Billing", "VTT", "Audio"]) expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
  });
});
