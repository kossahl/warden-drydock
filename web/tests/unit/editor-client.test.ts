import { digest, httpEditorApi, nextConnectionId, recomputeRecordDigest, type EditorRecord } from "../../src/editor/editorClient";

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

  it("matches Python ensure_ascii canonical digests for non-ASCII and astral Unicode", async () => {
    expect(await digest({ text: "café 😀", "\uE000": "bmp", "\u{10000}": "astral" })).toBe("a32d1782b2ae0836150433ce5190c088fa8197ccca2f4748adc8834f057168c4");
  });

  it("matches Python exponent formatting for floating-point request values", async () => {
    expect(await digest({ number: 1e-7 })).toBe("ebdf2f1d26e9cdfbd84490d407c41600abee1cfe8792a692d830accba5158fdc");
    expect(await digest({ number: 1.23e-6 })).toBe("6c52039bf8c1802b0613c78e6b56d1568c8133fd61f24fadfebab0f6f123f87e");
    expect(await digest({ number: 1e16 })).toBe("dfb80c069e7dfe29608ae611c5574d1d197e619a2b03005e43d675ef644ce2ac");
    expect(await digest({ number: -0 })).toBe("37e0043fa06b9c3790cd32d713dfda30e7ce8217f30ef142d2732c18ea0024d2");
  });

  it("matches Python ASCII escaping for DEL", async () => {
    expect(await digest({ text: "\u007f" })).toBe("184974d3c8a62e7135d9a5e29522efd28a17e96dbe2668629f01d6291458e351");
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

  it("reuses the exact operation identity after a response is lost", async () => {
    const response = { contract_name: "editor_proposal_view", contract_version: 1 };
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("network response lost"))
      .mockResolvedValueOnce({
        ok: true, headers: new Headers(), json: async () => response,
        status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
      } as Response);
    vi.stubGlobal("fetch", fetchMock);
    const revision = { revision_id: "revision_retry", ordinal: 1, tree_digest: "d".repeat(64) };

    await expect(httpEditorApi.propose("edit", "campaign_retry", revision, record(), 7)).rejects.toThrow("network response lost");
    await httpEditorApi.propose("edit", "campaign_retry", revision, record(), 7);

    const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    const retryBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(retryBody).toEqual(firstBody);
    expect(retryBody.operation_request.request_id).toBe(firstBody.operation_request.request_id);
    expect(retryBody.operation_request.idempotency_key).toBe(firstBody.operation_request.idempotency_key);
  });

  it("reuses the exact operation identity after a malformed response body", async () => {
    const response = { contract_name: "editor_proposal_view", contract_version: 1 };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true, headers: new Headers(), json: async () => { throw new SyntaxError("Unexpected end of JSON input"); },
        status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true, headers: new Headers(), json: async () => response,
        status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
      } as Response);
    vi.stubGlobal("fetch", fetchMock);
    const revision = { revision_id: "revision_malformed", ordinal: 1, tree_digest: "e".repeat(64) };

    await expect(httpEditorApi.propose("edit", "campaign_malformed", revision, record(), 7)).rejects.toThrow("Unexpected end of JSON input");
    await httpEditorApi.propose("edit", "campaign_malformed", revision, record(), 7);

    const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    const retryBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(retryBody).toEqual(firstBody);
    expect(retryBody.operation_request.request_id).toBe(firstBody.operation_request.request_id);
    expect(retryBody.operation_request.idempotency_key).toBe(firstBody.operation_request.idempotency_key);
  });

  it("retains the exact operation identity after an in-progress response", async () => {
    const response = { contract_name: "editor_proposal_view", contract_version: 1 };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: false, headers: new Headers(), json: async () => ({ error: { code: "operation_in_progress", category: "service_unavailable" } }),
        status: 503, statusText: "Service Unavailable", redirected: false, type: "basic", url: "",
      } as Response)
      .mockResolvedValueOnce({
        ok: true, headers: new Headers(), json: async () => response,
        status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
      } as Response);
    vi.stubGlobal("fetch", fetchMock);
    const revision = { revision_id: "revision_in_progress", ordinal: 1, tree_digest: "f".repeat(64) };

    await expect(httpEditorApi.propose("edit", "campaign_in_progress", revision, record(), 7)).rejects.toThrow("operation_in_progress");
    await httpEditorApi.propose("edit", "campaign_in_progress", revision, record(), 7);

    const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    const retryBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(retryBody).toEqual(firstBody);
  });

  it("reserves one operation identity for identical concurrent mutations", async () => {
    const response = { contract_name: "editor_proposal_view", contract_version: 1 };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true, headers: new Headers(), json: async () => response,
      status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
    }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const revision = { revision_id: "revision_concurrent", ordinal: 1, tree_digest: "a".repeat(64) };

    await Promise.all([
      httpEditorApi.propose("edit", "campaign_concurrent", revision, record(), 7),
      httpEditorApi.propose("edit", "campaign_concurrent", revision, record(), 7),
    ]);

    const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    const secondBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(secondBody).toEqual(firstBody);
  });

  it("reuses a pending operation identity after a module reload", async () => {
    const response = { contract_name: "editor_proposal_view", contract_version: 1 };
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("network response lost"))
      .mockResolvedValueOnce({
        ok: true, headers: new Headers(), json: async () => response,
        status: 201, statusText: "Created", redirected: false, type: "basic", url: "",
      } as Response);
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
    const revision = { revision_id: "revision_reload", ordinal: 1, tree_digest: "1".repeat(64) };

    await expect(httpEditorApi.propose("edit", "campaign_reload", revision, record(), 7)).rejects.toThrow("network response lost");
    expect(localStorage.length).toBe(1);
    const stored = localStorage.getItem(localStorage.key(0) ?? "") ?? "";
    expect(stored).not.toContain("campaign_reload");
    expect(stored).not.toContain("record-one");

    vi.resetModules();
    const reloadedClient = await import("../../src/editor/editorClient");
    await reloadedClient.httpEditorApi.propose("edit", "campaign_reload", revision, record(), 7);

    const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    const retryBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(retryBody).toEqual(firstBody);
    expect(localStorage.length).toBe(0);
  });
});
