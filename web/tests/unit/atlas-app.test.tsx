import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "../../src/App";
import type { AtlasApi } from "../../src/api/atlasClient";
import { ApiError } from "../../src/api/client";
import { binding, campaigns, detail, fullHistory, headRevision, neighborhood, newestFiveHistory, oldRevision, overview, readinessUnavailable, recordHistory, records, workflow } from "../fixtures/atlas";

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
    ...overrides,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => { resolve = finish; });
  return { promise, resolve };
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
    expect(screen.queryByRole("button", { name: /Ask|Check|Generate/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Non-canon work" })).toBeVisible();
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
    expect(screen.getByRole("link", { name: "brief" })).toHaveAttribute("href", "https://example.test/brief");
    expect(screen.queryByRole("link", { name: "trap" })).not.toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    fireEvent.click(screen.getByText("Exact source text"));
    expect(screen.getByText(/javascript:alert/)).toBeVisible();
    expect(screen.getByText("1 explicit relationship. Relationship map and list controls are reserved for the next Atlas package.")).toBeVisible();
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
    expect(await screen.findByRole("alert")).toHaveTextContent("requested Atlas view was not found");
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

  it("activates server cursors, clears them on filters, and keeps prior results while loading", async () => {
    const pending = deferred<typeof records>();
    const filtered = deferred<typeof records>();
    let calls = 0;
    const api = fakeAtlas({ records: vi.fn(async () => { calls += 1; return calls === 1 ? records : calls === 2 ? pending.promise : filtered.promise; }) });
    window.history.replaceState(null, "", "/campaigns/campaign_atlas/records?revision=revision_two");
    render(<App atlasApi={api} providerReadiness={async () => readinessUnavailable} />);
    await screen.findByText("2 matching records.");
    fireEvent.click(screen.getByRole("link", { name: "Next" }));
    expect(window.location.search).toContain("cursor=cursor_next");
    expect(screen.getByRole("link", { name: "Station Keeper" })).toBeVisible();
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
});
