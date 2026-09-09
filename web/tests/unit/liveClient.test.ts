import { digest } from "../../src/api/digest";
import { httpCaptureTransport } from "../../src/api/liveClient";
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
  it("binds capture requests to the complete public live payload", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ outcome: "accepted", session: { workflow_version: 3 } }),
    }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      const capture = await new MemoryCaptureStore("device_alpha").saveCapture(input);
      await expect(httpCaptureTransport.sendCapture(capture, capture.workflowVersion)).resolves.toEqual({ outcome: "accepted", workflowVersion: 3 });
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
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
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
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
      const firstCall = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      const secondCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      const firstBody = JSON.parse(firstCall[1].body as string) as { operation_request: { idempotency_key: string } };
      const secondBody = JSON.parse(secondCall[1].body as string) as { operation_request: { idempotency_key: string } };
      expect(firstBody.operation_request.idempotency_key).not.toBe(secondBody.operation_request.idempotency_key);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
