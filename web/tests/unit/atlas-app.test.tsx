import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
});
