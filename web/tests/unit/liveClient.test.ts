import { canonicalJson, digest } from "../../src/api/digest";
import { httpCaptureTransport, httpLiveClient } from "../../src/api/liveClient";
import { MemoryCaptureStore, type CaptureInput } from "../../src/live/captureStore";

const input: CaptureInput = {
  campaignId: "campaign_alpha",
  sessionId: "session_alpha",
  baseRevision: "revision_12",
  controllerId: "controller_alpha",
  controllerEpoch: 1,
  workflowVersion: 2,
  captureType: "confirmed_fact",
  text: "The door opened.",
  recordId: "record-one",
  eventId: "event_alpha",
  operationId: "operation_alpha",
};

describe("live capture HTTP transport", () => {
  it("uses only the documented live start, observe, and takeover contract fields", async () => {
    const session = {
      contract_name: "live_session_view", contract_version: 2, session_id: "session_alpha", campaign_id: "campaign_alpha",
      base_revision: "revision_12", reported_head_revision: "revision_12", workflow_version: 1,
      controller: { epoch: 1, controller_id: "controller_alpha", mode: "controller" }, mode: "active", events: [], acknowledgements: [], end_barrier: null,
      overlay: { overlay_id: "overlay_alpha", authority: "non_canon", base_revision: "revision_12", confirmed_fact_ids: [], question_ids: [] },
    };
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => ({
      ok: true, status: init?.method === "POST" ? 201 : 200, headers: new Headers(), json: async () => session,
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      await httpLiveClient.start("campaign_alpha", "revision_12", "controller_alpha");
      const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit?]>;
      const start = calls.find(([, init]) => init?.method === "POST")!;
      const startBody = JSON.parse(start[1]!.body as string) as Record<string, unknown>;
      const startInput = { campaign_id: "campaign_alpha", session_id: startBody.session_id, head_revision: "revision_12", controller_id: "controller_alpha" };
      expect(start[0]).toBe("/api/v1/campaigns/campaign_alpha/live/session");
      expect(startBody).toMatchObject({ contract_name: "live_start_request", contract_version: 2, ...startInput });
      expect(startBody.operation_request).toMatchObject({ operation: "live_start", payload_digest: await digest(startInput), expected_revision: null, expected_workflow_version: null });

      await httpLiveClient.observe("campaign_alpha");
      await httpLiveClient.takeover(session as never, "controller_beta");
      const last = calls.at(-1)!;
      const takeover = JSON.parse(last[1]!.body as string) as Record<string, unknown>;
      const takeoverInput = { campaign_id: "campaign_alpha", session_id: "session_alpha", controller_id: "controller_beta", controller_epoch: 1 };
      expect(calls[1][0]).toBe("/api/v1/campaigns/campaign_alpha/live/session");
      expect(last[0]).toBe("/api/v1/campaigns/campaign_alpha/live/session/takeover");
      expect(takeover).toMatchObject({ contract_name: "live_takeover_request", contract_version: 2, ...takeoverInput });
      expect(takeover.operation_request).toMatchObject({ operation: "live_takeover", payload_digest: await digest(takeoverInput), expected_workflow_version: 1 });
    } finally { vi.unstubAllGlobals(); }
  });

  it("binds capture requests to the complete public live payload", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers([["X-CSRF-Token", "csrf_alpha"]]),
      json: async () => ({ outcome: "accepted", session: { workflow_version: 3 } }),
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      const capture = await new MemoryCaptureStore("device_alpha").saveCapture(input);
      await expect(httpCaptureTransport.sendCapture(capture, capture.workflowVersion)).resolves.toEqual({ outcome: "accepted", workflowVersion: 3 });
      const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
      const postCall = calls.find(([, init]) => init.method === "POST")!;
      const [url, init] = postCall;
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      const operationRequest = body.operation_request as Record<string, unknown>;
      const expectedInput = {
        campaign_id: capture.campaignId,
        session_id: capture.sessionId,
        controller_id: capture.controllerId,
        controller_epoch: capture.controllerEpoch,
        event_id: capture.eventId,
        device_id: capture.deviceId,
        operation_id: capture.operationId,
        device_order: capture.deviceOrder,
        capture_type: capture.captureType,
        text: capture.text,
        record_id: capture.recordId,
      };
      expect(url).toBe("/api/v1/campaigns/campaign_alpha/live/session/captures");
      expect(operationRequest.payload_digest).toBe(await digest(expectedInput));
      expect(body).toMatchObject({ contract_name: "live_capture_request", contract_version: 2, ...expectedInput });
      expect(operationRequest).toMatchObject({ operation: "live_capture", expected_workflow_version: capture.workflowVersion });
      expect(operationRequest.idempotency_key).toMatch(/^live_capture_[a-f0-9]{64}$/);
      expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf_alpha");
      expect(canonicalJson({ text: "café 🚀" })).toBe('{"text":"caf\\u00e9 \\ud83d\\ude80"}');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("maps the exact local operation set into the end barrier request", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ workflow_version: 3, end_barrier: { ready_for_proposal: true } }),
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      const store = new MemoryCaptureStore("device_alpha");
      const end = await store.saveEnd({
        campaignId: "campaign_alpha",
        sessionId: "session_alpha",
        baseRevision: "revision_12",
        controllerId: "controller_alpha",
        controllerEpoch: 1,
        workflowVersion: 2,
        operationId: "operation_end",
        requiredOperationIds: [{ deviceId: "device_alpha", operationId: "operation_alpha" }],
      });
      await expect(httpCaptureTransport.sendEnd(end, end.workflowVersion)).resolves.toEqual({ readyForProposal: true, workflowVersion: 3 });
      const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
      const postCall = calls.find(([, init]) => init.method === "POST")!;
      const [url, init] = postCall;
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      const operationRequest = body.operation_request as Record<string, unknown>;
      const expectedInput = {
        campaign_id: end.campaignId,
        session_id: end.sessionId,
        controller_id: end.controllerId,
        controller_epoch: end.controllerEpoch,
        device_id: end.deviceId,
        operation_id: end.operationId,
        required_operation_ids: [{ device_id: "device_alpha", operation_id: "operation_alpha" }],
      };
      expect(url).toBe("/api/v1/campaigns/campaign_alpha/live/session/end");
      expect(operationRequest.payload_digest).toBe(await digest(expectedInput));
      expect(body).toMatchObject({ contract_name: "live_end_request", contract_version: 2, ...expectedInput });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("scopes capture idempotency keys to session, device, and operation", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ outcome: "accepted", session: { workflow_version: 2 } }),
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      const first = await new MemoryCaptureStore("device_alpha").saveCapture(input);
      const second = await new MemoryCaptureStore("device_beta").saveCapture(input);
      await httpCaptureTransport.sendCapture(first, first.workflowVersion);
      await httpCaptureTransport.sendCapture(second, second.workflowVersion);
      const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
      const postCalls = calls.filter(([, init]) => init.method === "POST");
      const firstCall = postCalls[0];
      const secondCall = postCalls[1];
      const firstBody = JSON.parse(firstCall[1].body as string) as { operation_request: { idempotency_key: string } };
      const secondBody = JSON.parse(secondCall[1].body as string) as { operation_request: { idempotency_key: string } };
      expect(firstBody.operation_request.idempotency_key).not.toBe(secondBody.operation_request.idempotency_key);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("preserves receipt digests, capture identities, and terminal session mode", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({
        session_id: "session_alpha",
        workflow_version: 4,
        mode: "ended_review_pending",
        controller: { controller_id: "controller_beta", epoch: 3, mode: "controller" },
        end_barrier: {
          end_device_id: "device_alpha",
          end_operation_id: "operation_end",
          required_operation_ids: [],
          acknowledged_operation_ids: [],
          ready_for_proposal: true,
        },
        events: [{ device_id: "device_alpha", operation_id: "operation_alpha" }],
        acknowledgements: [{ device_id: "device_alpha", operation_id: "operation_alpha", payload_digest: "a".repeat(64), outcome: "accepted" }],
      }),
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(httpCaptureTransport.readSession!("campaign_alpha", "session_alpha")).resolves.toEqual({
        workflowVersion: 4,
        controllerId: "controller_beta",
        controllerEpoch: 3,
        endBarrier: { deviceId: "device_alpha", operationId: "operation_end", readyForProposal: true },
        acknowledgedOperationIds: [{ deviceId: "device_alpha", operationId: "operation_alpha" }],
        acknowledgements: [{ deviceId: "device_alpha", operationId: "operation_alpha", payloadDigest: "a".repeat(64), outcome: "accepted" }],
        captureOperationIds: [{ deviceId: "device_alpha", operationId: "operation_alpha" }],
        mode: "ended_review_pending",
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
