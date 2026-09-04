import { httpEditorApi, nextConnectionId, recomputeRecordDigest, type EditorRecord } from "../../src/editor/editorClient";

const record = (): EditorRecord => ({
  record_id: "record-one", record_type: "npc", displayed_name: "One", status: "draft", authority: "preparation",
  visibility: { audience: "warden", warden_only: true }, fields: [], sections: [{ section_id: "summary", body: "Text" }],
  connections: [], content_digest: "0".repeat(64),
});

describe("record editor client bindings", () => {
  it("recomputes the typed record digest without trusting the wire digest", async () => {
    const first = await recomputeRecordDigest(record());
    const changed = record(); changed.content_digest = "f".repeat(64); changed.displayed_name = "Changed";
    expect(await recomputeRecordDigest(changed)).not.toBe(first);
    expect(first).toMatch(/^[a-f0-9]{64}$/);
  });

  it("allocates unique public connection IDs after removal", () => {
    const connections = [
      { connection_id: "connection_1", target_record_id: "one", relationship: "related-to", state: "current", context: "One" },
      { connection_id: "connection_3", target_record_id: "three", relationship: "related-to", state: "current", context: "Three" },
    ];
    const next = nextConnectionId(connections);
    expect(next).toBe("connection_4");
    expect(next).toMatch(/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/);
  });

  it("sends a closed removal request and carries the CSRF token after the first response", async () => {
    const response = { contract_name: "editor_proposal_view", contract_version: 1 };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => ({
      ok: true, headers: new Headers({ "X-CSRF-Token": "csrf-token" }), json: async () => response,
      status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
    }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const revision = { revision_id: "revision_one", ordinal: 1, tree_digest: "a".repeat(64) };
    const impact = { contract_name: "editor_removal_impact" as const, contract_version: 1 as const,
      binding: { campaign_id: "campaign_one", base_revision: revision, record_id: "record-one", record_digest: "b".repeat(64), expected_editor_workflow_version: 1 },
      impact_digest: "c".repeat(64), record: record(), outgoing_connections: [], incoming_references: [], backlink_policy: "server_derived_from_typed_connections" as const };
    await httpEditorApi.propose("remove", "campaign_one", revision, record(), 1, [], impact);
    await httpEditorApi.propose("remove", "campaign_one", revision, record(), 1, [], impact);
    const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(firstBody.candidate).toBeUndefined();
    expect(firstBody.operation_request.subject_id).toBe("record-one");
    expect(firstBody.operation_request.payload_digest).toMatch(/^[a-f0-9]{64}$/);
    expect(fetchMock.mock.calls[1][1]?.headers).toBeInstanceOf(Headers);
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("X-CSRF-Token")).toBe("csrf-token");
  });
});
