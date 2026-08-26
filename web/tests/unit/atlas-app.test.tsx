import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "../../src/App";
import type { AtlasApi } from "../../src/api/atlasClient";
import { ApiError, type SliceApi } from "../../src/api/client";
import type { GenerationView, ProposalView, ProviderReadiness } from "../../src/contracts/v2";
import { binding, campaigns, detail, fullHistory, generations, headRevision, neighborhood, newestFiveHistory, oldRevision, overview, proposals, readinessUnavailable, recordHistory, records, workflow } from "../fixtures/atlas";

function fakeAtlas(overrides: Partial<AtlasApi> = {}): AtlasApi {
  return {
    campaigns: vi.fn(async () => campaigns),
    resolveRevision: vi.fn(async (_campaign, revision) => revision === oldRevision.revision_id ? oldRevision : headRevision),
    overview: vi.fn(async () => overview),
    records: vi.fn(async () => records),
    record: vi.fn(async () => detail),
    neighborhood: vi.fn(async () => neighborhood),
    history: vi.fn(async (_campaign, query) => query.limit === 5 ? newestFiveHistory : query.subject_record_id ? recordHistory : fullHistory),
    workflow: vi.fn(async () => workflow),
    generations: vi.fn(async () => generations),
    proposals: vi.fn(async () => proposals),
    ...overrides,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => { resolve = finish; });
  return { promise, resolve };
}
const ready: ProviderReadiness = { contract_name: "provider_readiness_response", contract_version: 2, provider_configured: true, provider_available: true, consent_current: true, consent_identity_digest: "9".repeat(64), ai_available: true };
function generationApi(context: GenerationView["context"], action: GenerationView["action"] = "generate", sourceRevision = "revision_two") {
  const complete: GenerationView = { contract_name: "generation_view", contract_version: 2, generation_id: "generation_ui", campaign_id: "campaign_atlas", source_revision: sourceRevision, action, context, session_id: null, draft_authority: "draft", status: "complete", sources: [{ source_id: "record-one", authority: "canon", revision_id: sourceRevision, order: 1, excerpt: "Pinned excerpt", excerpt_digest: "1".repeat(64) }], source_set_digest: "2".repeat(64), last_sequence: 1, terminal_content: "A grounded Draft.", terminal_content_digest: "3".repeat(64) };
  return { readiness: vi.fn(async () => ready), consent: vi.fn(async () => ready), startGeneration: vi.fn(async () => ({ ...complete, status: "pending", terminal_content: null, terminal_content_digest: null })), resumeGeneration: vi.fn(async () => []), readGeneration: vi.fn(async () => complete), createProposal: vi.fn(async () => ({ proposal_id: "proposal_ui", proposal_version: 1 } as ProposalView)) } as unknown as SliceApi;
}

describe("Campaign Atlas browser experience", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("keeps Atlas usable during provider outage and shows only the newest five approved revisions", async () => {
    const api = fakeAtlas();
    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=revision_two");
    render(<App atlasApi={api} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Synthetic Atlas" })).toBeVisible();
    expect(screen.getByText("Provider: Unavailable")).toBeVisible();
    const recent = screen.getByRole("heading", { name: "Most recent approved revisions" }).closest("section")!;
    expect(await within(recent).findAllByRole("listitem")).toHaveLength(5);
    expect(within(recent).getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Revision 6", "Revision 5", "Revision 4", "Revision 3", "Revision 2"]);
    expect(api.history).toHaveBeenCalledWith("campaign_atlas", expect.objectContaining({ limit: 5, direction: "backward" }));
    expect(screen.getByRole("button", { name: "Submit Ask" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "Drafts" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Proposals" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Accepted (legacy) (1)" })).toBeVisible();
  });

  it("clears a cursor when search changes and keeps filters in the URL", async () => {
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records?revision=revision_two&type=npc&cursor=stale_cursor");
    render(<App atlasApi={fakeAtlas()} providerReadiness={async () => readinessUnavailable} />);
    const search = await screen.findByLabelText("Search campaign records");
    await screen.findByText("2 matching records.");
    fireEvent.change(search, { target: { value: "station & keeper" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(window.location.search).toContain("q=station+%26+keeper"));
    expect(window.location.search).toContain("type=npc");
    expect(window.location.search).not.toContain("cursor=");
  });

  it("renders safe Markdown, rejects unsafe links, and discloses exact source text", async () => {
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    const { container } = render(<App atlasApi={fakeAtlas()} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Station Keeper" })).toBeVisible();
    const recordContent = screen.getByRole("heading", { name: "Record content" }).closest("section")!;
    const renderedContent = recordContent.querySelector(".markdown")!;
    expect(within(renderedContent as HTMLElement).getByRole("link", { name: "brief" })).toHaveAttribute("href", "https://example.test/brief");
    expect(within(renderedContent as HTMLElement).queryByRole("link", { name: "trap" })).not.toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(await within(renderedContent as HTMLElement).findByText(/The keeper relies on/)).toBeVisible();
    expect(within(renderedContent as HTMLElement).getByRole("link", { name: "Legacy Ship" })).toHaveAttribute("href", expect.stringContaining("revision=revision_two"));
    expect(renderedContent).not.toHaveTextContent("`knows`");
    expect(renderedContent).not.toHaveTextContent("[[record-two|Legacy Ship]]");
    fireEvent.click(within(recordContent).getByText("Exact source text"));
    expect(within(recordContent).getByText(/javascript:alert/)).toBeVisible();
    expect(recordContent.querySelector("pre")).toHaveTextContent("- `knows` → [[record-two|Legacy Ship]] (`current`) — The keeper relies on Legacy Ship.");
    const relationships = screen.getByRole("heading", { name: "Relationships" }).closest("section")!;
    expect(within(relationships).getByRole("button", { name: "Map", pressed: true })).toBeVisible();
    expect(within(relationships).getAllByRole("link", { name: "Legacy Ship" })).toHaveLength(2);
    const card = relationships.querySelector(".relationship-card")!;
    expect(card.firstElementChild).toHaveTextContent("The keeper relies on Legacy Ship.");
    expect(card).toHaveTextContent("Related record: Legacy Ship");
    expect(card).toHaveTextContent("DirectionOutgoing");
    expect(card).toHaveTextContent("TypeKnows");
    expect(card).toHaveTextContent("StateCurrent");
    expect(card).not.toHaveTextContent("Station Keeper knows Legacy Ship");
  });

  it("replaces an uppercase Connections heading without exposing its DSL", async () => {
    const uppercaseDetail = { ...detail, record: { ...detail.record, content: detail.record.content.replace("## Connections", "## CONNECTIONS") } };
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    render(<App atlasApi={fakeAtlas({ record: vi.fn(async () => uppercaseDetail) })} providerReadiness={async () => readinessUnavailable} />);
    const recordContent = (await screen.findByRole("heading", { name: "Record content" })).closest("section")!;
    const renderedContent = recordContent.querySelector(".markdown")!;
    expect(within(renderedContent as HTMLElement).getByRole("heading", { name: "CONNECTIONS" })).toBeVisible();
    expect(await within(renderedContent as HTMLElement).findByRole("link", { name: "Legacy Ship" })).toBeVisible();
    expect(renderedContent).not.toHaveTextContent("[[record-two|Legacy Ship]]");
  });

  it("keeps a historical revision selected until Open head", async () => {
    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=revision_one");
    const api = fakeAtlas({ overview: vi.fn(async () => ({ ...overview, binding: { ...binding, viewed_revision: oldRevision } })) });
    render(<App atlasApi={api} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByText(/Viewed revision 1/)).toBeVisible();
    fireEvent.click(screen.getByRole("link", { name: "Open head" }));
    await waitFor(() => expect(window.location.search).toBe("?revision=revision_two"));
    expect(await screen.findByText(/Viewed revision 2/)).toBeVisible();
  });

  it("blocks the whole affected campaign view after an integrity failure", async () => {
    let calls = 0;
    const api = fakeAtlas({ records: vi.fn(async () => { calls += 1; if (calls > 1) throw new ApiError(409, "snapshot_integrity_failure"); return records; }) });
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records?revision=revision_two");
    render(<App atlasApi={api} providerReadiness={async () => readinessUnavailable} />);
    await screen.findByText("2 matching records.");
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "npc" } });
    expect(await screen.findByRole("heading", { level: 1, name: "Campaign view blocked" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Station Keeper" })).not.toBeInTheDocument();
  });

  it("moves focus back to the preserved root workspace", async () => {
    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=revision_two");
    render(<App atlasApi={fakeAtlas()} providerReadiness={async () => readinessUnavailable} />);
    fireEvent.click(await screen.findByRole("link", { name: "Warden Drydock proposal workspace" }));
    await waitFor(() => expect(document.querySelector("#main-content")).toHaveFocus());
  });

  it("does not issue an Atlas read until the exact historical revision binding resolves", async () => {
    const pending = deferred<{ revision_id: string; ordinal: number; tree_digest: string }>();
    const readOverview = vi.fn(async () => overview);
    const api = fakeAtlas({ resolveRevision: vi.fn(async () => pending.promise), overview: readOverview });
    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=revision_two");
    render(<App atlasApi={api} providerReadiness={async () => readinessUnavailable} />);
    const recent = await screen.findByRole("heading", { name: "Most recent approved revisions" });
    await within(recent.closest("section")!).findAllByRole("listitem");
    readOverview.mockClear();
    fireEvent.click(screen.getByRole("link", { name: "revision_5" }));
    expect(await screen.findByRole("heading", { name: "Resolving revision" })).toBeVisible();
    expect(readOverview).not.toHaveBeenCalled();
    await act(async () => pending.resolve({ revision_id: "revision_5", ordinal: 5, tree_digest: "5".repeat(64) }));
    await waitFor(() => expect(readOverview).toHaveBeenCalledWith("campaign_atlas", expect.objectContaining({ revision_id: "revision_5", revision_ordinal: 5 })));
  });

  it("offers recoverable states for unknown campaign, revision, and record without echoing unsafe identifiers", async () => {
    window.history.replaceState(null, "", "/campaigns/private-campaign?revision=revision_two");
    const { unmount } = render(<App atlasApi={fakeAtlas()} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByRole("heading", { name: "Campaign unavailable" })).toBeVisible();
    expect(screen.queryByText("private-campaign")).not.toBeInTheDocument();
    unmount();

    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=missing_revision");
    const revisionApi = fakeAtlas({ resolveRevision: vi.fn(async () => { throw new ApiError(404, "not_found"); }) });
    const second = render(<App atlasApi={revisionApi} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByRole("heading", { name: "Revision unavailable" })).toBeVisible();
    expect(screen.queryByText("missing_revision")).not.toBeInTheDocument();
    second.unmount();

    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/private-record?revision=revision_two");
    const recordApi = fakeAtlas({ record: vi.fn(async () => { throw new ApiError(404, "not_found"); }) });
    render(<App atlasApi={recordApi} providerReadiness={async () => readinessUnavailable} />);
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent("requested Atlas view was not found");
    expect(screen.queryByText("private-record")).not.toBeInTheDocument();
  });

  it.each([
    ["rebuild_required", "must be rebuilt"],
    ["integrity_blocked", "Snapshot integrity verification blocks"],
  ] as const)("blocks campaign-list recovery state %s", async (recoveryState, message) => {
    const collection = { ...campaigns, campaigns: [{ ...campaigns.campaigns[0], recovery_state: recoveryState }] };
    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=revision_two");
    render(<App atlasApi={fakeAtlas({ campaigns: vi.fn(async () => collection) })} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByRole("heading", { name: "Campaign view unavailable" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(message);
  });

  it("distinguishes an empty revision from filters with no matches", async () => {
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records?revision=revision_two");
    const empty = { ...records, total: 0, items: [], facets: { record_types: [], authorities: [], statuses: [] } };
    const first = render(<App atlasApi={fakeAtlas({ records: vi.fn(async () => empty) })} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByText("No records are stored in this revision.")).toBeVisible();
    first.unmount();
    const noMatches = { ...records, total: 0, items: [] };
    render(<App atlasApi={fakeAtlas({ records: vi.fn(async () => noMatches) })} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByText("No records match this search and filter combination.")).toBeVisible();
  });

  it("activates server cursors, clears them on filters, and suppresses stale results while loading", async () => {
    const pending = deferred<typeof records>();
    const filtered = deferred<typeof records>();
    let calls = 0;
    const api = fakeAtlas({ records: vi.fn(async () => { calls += 1; return calls === 1 ? records : calls === 2 ? pending.promise : filtered.promise; }) });
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records?revision=revision_two");
    render(<App atlasApi={api} providerReadiness={async () => readinessUnavailable} />);
    await screen.findByText("2 matching records.");
    fireEvent.click(screen.getByRole("link", { name: "Next" }));
    expect(window.location.search).toContain("cursor=cursor_next");
    expect(screen.queryByRole("link", { name: "Station Keeper" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Records" }).closest("section")).toHaveAttribute("aria-busy", "true");
    await act(async () => pending.resolve({ ...records, previous_cursor: "cursor_previous", next_cursor: null }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Records" }).closest("section")).toHaveAttribute("aria-busy", "false"));
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "npc" } });
    expect(window.location.search).not.toContain("cursor=");
  });

  it("links removed records to the supplied parent revision", async () => {
    const removed = { ...fullHistory, entries: [{ ...fullHistory.entries[0], changes: [{ ...fullHistory.entries[0].changes[0], change_kind: "removed" as const, link_revision_id: "revision_one", after_content_digest: null, after_status: null, to_authority: null }], affected_records: [{ record_id: "record-one", link_revision_id: "revision_one" }] }] };
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/history?revision=revision_two");
    render(<App atlasApi={fakeAtlas({ history: vi.fn(async () => removed) })} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByRole("link", { name: "Removed record record-one" })).toHaveAttribute("href", expect.stringContaining("revision=revision_one"));
  });

  it("preserves duplicate and self relationship occurrences and resets only the relationship cursor on navigation", async () => {
    const edges = [
      { ...neighborhood.edges[0], edge_id: `edge_${"1".repeat(64)}`, occurrence_order: 1 },
      { ...neighborhood.edges[0], edge_id: `edge_${"2".repeat(64)}`, occurrence_order: 2, context: "Second occurrence." },
      { ...neighborhood.edges[0], edge_id: `edge_${"3".repeat(64)}`, occurrence_order: 3, source_record_id: "record-one", target_record_id: "record-one", relationship: "remembers", context: "Self occurrence." },
    ];
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two&q=station&type=npc&cursor=library_page&relationship_cursor=edge_page&generation_cursor=draft_page&proposal_cursor=proposal_page");
    const { container } = render(<App atlasApi={fakeAtlas({ neighborhood: vi.fn(async () => ({ ...neighborhood, edges, total_edges: 3 })) })} providerReadiness={async () => readinessUnavailable} />);
    await screen.findByRole("heading", { name: "Station Keeper", level: 1 });
    expect(container.querySelectorAll(".relationship-list > li")).toHaveLength(3);
    expect(container.querySelectorAll(".relationship-focus .relationship-card")).toHaveLength(1);
    const neighbor = container.querySelector<HTMLAnchorElement>(".relationship-list a")!;
    expect(neighbor.href).toContain("q=station"); expect(neighbor.href).toContain("type=npc"); expect(neighbor.href).toContain("cursor=library_page"); expect(neighbor.href).not.toContain("relationship_cursor"); expect(neighbor.href).not.toContain("generation_cursor"); expect(neighbor.href).not.toContain("proposal_cursor");
    expect(screen.getByRole("group", { name: "Relationship view" })).toBeVisible();
    const listButton = screen.getByRole("button", { name: "List" }); fireEvent.click(listButton); expect(listButton).toHaveFocus(); expect(listButton).toHaveAttribute("aria-pressed", "true");
    const selfRow = container.querySelectorAll(".relationship-list > li")[2];
    expect(selfRow).toHaveTextContent("Related record: Station Keeper (this record)");
    expect(within(selfRow as HTMLElement).getByRole("link", { name: "Station Keeper" })).toHaveAttribute("href", expect.stringContaining("revision=revision_two"));
  });

  it("labels incoming backlinks and fails closed when a viewed-revision endpoint is missing", async () => {
    const incoming = { ...neighborhood, edges: [{ ...neighborhood.edges[0], source_record_id: "record-two", target_record_id: "record-one", relationship: "reports-to", state: "former", context: "The ship once reported to the keeper." }] };
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    const { rerender } = render(<App atlasApi={fakeAtlas({ neighborhood: vi.fn(async () => incoming) })} providerReadiness={async () => readinessUnavailable} />);
    const relationships = (await screen.findByRole("heading", { name: "Relationships" })).closest("section")!;
    await within(relationships).findAllByRole("link", { name: "Legacy Ship" });
    const card = relationships.querySelector(".relationship-card")!;
    expect(card.firstElementChild).toHaveTextContent("The ship once reported to the keeper.");
    expect(card).toHaveTextContent("Source: Legacy Ship");
    expect(card).toHaveTextContent("DirectionIncoming");
    expect(card).toHaveTextContent("TypeReports To");
    const recordContent = screen.getByRole("heading", { name: "Record content" }).closest("section")!;
    expect(within(recordContent).getByText("No explicit connections in this record.")).toBeVisible();

    const missing = { ...neighborhood, neighbors: [] };
    rerender(<App atlasApi={fakeAtlas({ neighborhood: vi.fn(async () => missing) })} providerReadiness={async () => readinessUnavailable} />);
    await waitFor(() => {
      const currentContent = screen.getByRole("heading", { name: "Record content" }).closest("section")!;
      expect(within(currentContent).getByRole("alert")).toHaveTextContent("Connection details are unavailable for this revision.");
    });
    const relationshipSection = screen.getByRole("heading", { name: "Relationships" }).closest("section")!;
    expect(within(relationshipSection).getByRole("alert")).toHaveTextContent("Relationship data could not be verified.");
    expect(screen.queryByRole("link", { name: "Legacy Ship" })).not.toBeInTheDocument();
    expect(screen.queryByText("record-two")).not.toBeInTheDocument();
  });

  it("builds Record content from every source-owned relationship page without showing backlinks", async () => {
    const firstPage = {
      ...neighborhood,
      edges: [
        { ...neighborhood.edges[0], edge_id: `edge_${"1".repeat(64)}`, occurrence_order: 1, context: "The keeper relies on Legacy Ship." },
        { ...neighborhood.edges[0], edge_id: `edge_${"2".repeat(64)}`, occurrence_order: 2, source_record_id: "record-two", target_record_id: "record-one", context: "Legacy Ship answers to the keeper." },
      ],
      total_edges: 3,
      next_cursor: "relationship_page_two",
    };
    const secondPage = {
      ...neighborhood,
      edges: [{ ...neighborhood.edges[0], edge_id: `edge_${"3".repeat(64)}`, occurrence_order: 3, context: "Legacy Ship carries the keeper's emergency beacon." }],
      total_edges: 3,
      next_cursor: null,
      previous_cursor: "relationship_page_one",
    };
    const readNeighborhood = vi.fn(async (_campaign: string, _record: string, query: { cursor?: string }) => query.cursor === "relationship_page_two" ? secondPage : firstPage);
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two&relationship_cursor=visible_page");
    const { container } = render(<App atlasApi={fakeAtlas({ neighborhood: readNeighborhood })} providerReadiness={async () => readinessUnavailable} />);
    const recordContent = (await screen.findByRole("heading", { name: "Record content" })).closest("section")!;
    await waitFor(() => expect(recordContent.querySelectorAll(".record-connection-list > li")).toHaveLength(2));
    const statements = recordContent.querySelectorAll(".record-connection-list > li");
    expect(statements[0]).toHaveTextContent("The keeper relies on Legacy Ship.");
    expect(statements[1]).toHaveTextContent("Legacy Ship carries the keeper's emergency beacon.");
    expect(recordContent).not.toHaveTextContent("Legacy Ship answers to the keeper.");
    expect(recordContent.querySelector(".markdown")).not.toHaveTextContent("[[record-two|Legacy Ship]]");
    expect(readNeighborhood).toHaveBeenCalledWith("campaign_atlas", "record-one", expect.objectContaining({ cursor: "relationship_page_two" }));
    expect(container.querySelector(".relationships")).toBeInTheDocument();
  });

  it("pins relationship target navigation to a historical viewed revision", async () => {
    const historicalBinding = { ...binding, viewed_revision: oldRevision };
    const historicalDetail = { ...detail, binding: historicalBinding };
    const historicalNeighborhood = { ...neighborhood, binding: historicalBinding };
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_one");
    render(<App atlasApi={fakeAtlas({ record: vi.fn(async () => historicalDetail), neighborhood: vi.fn(async () => historicalNeighborhood) })} providerReadiness={async () => readinessUnavailable} />);
    const target = (await screen.findAllByRole("link", { name: "Legacy Ship" }))[0];
    expect(target).toHaveAttribute("href", expect.stringContaining("revision=revision_one"));
  });

  it("shows persisted workflow bindings and publication-safe status links", async () => {
    const generationRows = { ...generations, items: [{ generation_id: "generation_one", action: "check" as const, context: { scope: "campaign" as const }, source_revision: headRevision, source_set_digest: "4".repeat(64), status: "complete" as const, retryable: null, created_at: "2026-08-25T08:00:00Z" }, { generation_id: "generation_failed", action: "ask" as const, context: { scope: "campaign" as const }, source_revision: headRevision, source_set_digest: "5".repeat(64), status: "failed" as const, retryable: true, created_at: "2026-08-25T07:00:00Z" }] };
    const proposalRows = { ...proposals, items: [{ proposal_id: "proposal_one", proposal_version: 2, generation_id: "generation_two", action: "generate" as const, context: { scope: "record" as const, record_id: "record-one", content_digest: detail.record.content_digest }, subject_record_id: "record-one", subject_content_digest: detail.record.content_digest, source_revision: headRevision, base_revision: oldRevision, status: "conflict" as const, validation_status: "passed" as const, published_revision_id: null, created_at: "2026-08-25T08:01:00Z" }] };
    window.history.replaceState(null, "", "/campaigns/campaign_atlas?revision=revision_two");
    render(<App atlasApi={fakeAtlas({ generations: vi.fn(async () => generationRows), proposals: vi.fn(async () => proposalRows) })} providerReadiness={async () => readinessUnavailable} />);
    expect(await screen.findByText("Draft ready, not canon")).toBeVisible();
    expect(screen.getByText("Conflict, not published")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "Open Draft" })[0]).toHaveAttribute("href", "/?generation=generation_one");
    expect(screen.getByRole("link", { name: "Review proposal" })).toHaveAttribute("href", "/?proposal=proposal_one&version=2");
    expect(screen.getByText("Provider reported this failure as retryable. Starting another inference requires a new explicit request.")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Retry (Draft|generation)/ })).not.toBeInTheDocument();
  });

  it("starts an explicit record Generate with the exact pinned digest and no session", async () => {
    const slice = generationApi({ scope: "record", record_id: "record-one", content_digest: detail.record.content_digest });
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    render(<App api={slice} atlasApi={fakeAtlas()} />);
    await screen.findByRole("heading", { name: "Station Keeper", level: 1 });
    fireEvent.click(screen.getByRole("radio", { name: "Generate" }));
    expect(slice.startGeneration).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Generation brief"), { target: { value: "Add a consequence" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit Generate" }));
    await screen.findByText("A grounded Draft.");
    expect(slice.startGeneration).toHaveBeenCalledWith("campaign_atlas", "revision_two", "generate", "Add a consequence", expect.any(String), { scope: "record", record_id: "record-one", content_digest: detail.record.content_digest });
    expect(screen.getByRole("button", { name: "Review as proposal for Station Keeper" })).toBeVisible();
  });

  it("replays an uncertain start with the same immutable generation request", async () => {
    const slice = generationApi({ scope: "record", record_id: "record-one", content_digest: detail.record.content_digest }, "ask");
    vi.mocked(slice.startGeneration).mockRejectedValueOnce(new Error("transport_lost"));
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    render(<App api={slice} atlasApi={fakeAtlas()} />);
    await screen.findByRole("heading", { name: "Station Keeper", level: 1 });
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "What changed?" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit Ask" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry exact start request" }));
    await screen.findByText("A grounded Draft.");
    expect(slice.startGeneration).toHaveBeenCalledTimes(2);
    expect(vi.mocked(slice.startGeneration).mock.calls[0]).toEqual(vi.mocked(slice.startGeneration).mock.calls[1]);
  });

  it("clears Grounded AI state when navigating from one record binding to another", async () => {
    const secondDetail = { ...detail, record: { ...detail.record, record_id: "record-two", name: "Legacy Ship", content_digest: "8".repeat(64) } };
    const secondNeighborhood = { ...neighborhood, focus: secondDetail.record, neighbors: [detail.record], edges: [{ ...neighborhood.edges[0], source_record_id: "record-two", target_record_id: "record-one" }] };
    const atlas = fakeAtlas({ record: vi.fn(async (_campaign, recordId) => recordId === "record-two" ? secondDetail : detail), neighborhood: vi.fn(async (_campaign, recordId) => recordId === "record-two" ? secondNeighborhood : neighborhood) });
    const slice = generationApi({ scope: "record", record_id: "record-one", content_digest: detail.record.content_digest });
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    render(<App api={slice} atlasApi={atlas} />);
    await screen.findByRole("heading", { name: "Station Keeper", level: 1 });
    fireEvent.click(screen.getByRole("radio", { name: "Generate" }));
    fireEvent.change(screen.getByLabelText("Generation brief"), { target: { value: "Draft for record one" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit Generate" }));
    await screen.findByText("A grounded Draft.");
    fireEvent.click(screen.getAllByRole("link", { name: "Legacy Ship" })[0]);
    await screen.findByRole("heading", { name: "Legacy Ship", level: 1 });
    expect(screen.queryByText("A grounded Draft.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Review as proposal/ })).not.toBeInTheDocument();
  });

  it("clears a historical Draft when opening the head revision", async () => {
    const slice = generationApi({ scope: "record", record_id: "record-one", content_digest: detail.record.content_digest }, "generate", "revision_one");
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_one");
    render(<App api={slice} atlasApi={fakeAtlas()} />);
    await screen.findByRole("heading", { name: "Station Keeper", level: 1 });
    fireEvent.click(screen.getByRole("radio", { name: "Generate" }));
    fireEvent.change(screen.getByLabelText("Generation brief"), { target: { value: "Historical draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit Generate" }));
    await screen.findByText("A grounded Draft.");
    fireEvent.click(screen.getByRole("link", { name: "Open head to create a proposal." }));
    await waitFor(() => expect(window.location.search).toBe("?revision=revision_two"));
    expect(screen.queryByText("A grounded Draft.")).not.toBeInTheDocument();
  });

  it("uses a new proposal identity for a new generation after an uncertain proposal result", async () => {
    const slice = generationApi({ scope: "record", record_id: "record-one", content_digest: detail.record.content_digest });
    vi.mocked(slice.createProposal).mockRejectedValueOnce(new Error("transport_lost"));
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records/record-one?revision=revision_two");
    render(<App api={slice} atlasApi={fakeAtlas()} />);
    await screen.findByRole("heading", { name: "Station Keeper", level: 1 });
    fireEvent.click(screen.getByRole("radio", { name: "Generate" }));
    fireEvent.change(screen.getByLabelText("Generation brief"), { target: { value: "First draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit Generate" }));
    fireEvent.click(await screen.findByRole("button", { name: "Review as proposal for Station Keeper" }));
    await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("Generation brief"), { target: { value: "Second draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit Generate" }));
    fireEvent.click(await screen.findByRole("button", { name: "Review as proposal for Station Keeper" }));
    await waitFor(() => expect(slice.createProposal).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(slice.createProposal).mock.calls;
    expect(calls[0][2]).not.toBe(calls[1][2]);
    expect(calls[0][3]).not.toBe(calls[1][3]);
  });
});
