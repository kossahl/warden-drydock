import { digest } from "./digest";
import { browserId, ensureCsrfToken, requestJson } from "./client";
import type { LiveCaptureResult, LiveSessionView, OperationRequest } from "../contracts/v2";
import type { CaptureSyncTransport, StoredCapture, StoredEndIntent } from "../live/captureStore";

export interface LiveClient {
  start(campaignId: string, headRevision: string, controllerId: string): Promise<LiveSessionView>;
  observe(campaignId: string): Promise<LiveSessionView>;
  takeover(session: LiveSessionView, controllerId: string): Promise<LiveSessionView>;
}

function operation(operationName: OperationRequest["operation"], idempotencyKey: string, payloadDigest: string, workflowVersion: number): OperationRequest {
  return {
    contract_name: "operation_request",
    contract_version: 2,
    request_id: browserId("request"),
    operation: operationName,
    idempotency_key: idempotencyKey,
    payload_digest: payloadDigest,
    expected_revision: null,
    expected_workflow_version: workflowVersion,
  };
}

function path(campaignId: string, suffix: string): string {
  return `/campaigns/${encodeURIComponent(campaignId)}/live/session${suffix}`;
}

async function liveIdempotencyKey(kind: "capture" | "end", sessionId: string, deviceId: string, operationId: string): Promise<string> {
  const identityDigest = await digest({ session_id: sessionId, device_id: deviceId, operation_id: operationId });
  return `live_${kind}_${identityDigest}`;
}

export const httpCaptureTransport: CaptureSyncTransport = {
  async sendCapture(capture: StoredCapture, workflowVersion: number) {
    await ensureCsrfToken();
    const input = {
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
    const result = await requestJson<LiveCaptureResult>(path(capture.campaignId, "/captures"), {
      method: "POST",
      body: JSON.stringify({
        contract_name: "live_capture_request",
        contract_version: 2,
        operation_request: operation("live_capture", await liveIdempotencyKey("capture", capture.sessionId, capture.deviceId, capture.operationId), await digest(input), workflowVersion),
        ...input,
      }),
    });
    return { outcome: result.outcome, workflowVersion: result.session.workflow_version };
  },

  async sendEnd(end: StoredEndIntent, workflowVersion: number) {
    await ensureCsrfToken();
    const input = {
      campaign_id: end.campaignId,
      session_id: end.sessionId,
      controller_id: end.controllerId,
      controller_epoch: end.controllerEpoch,
      device_id: end.deviceId,
      operation_id: end.operationId,
      required_operation_ids: end.requiredOperationIds.map(({ deviceId, operationId }) => ({ device_id: deviceId, operation_id: operationId })),
    };
    const result = await requestJson<LiveSessionView>(path(end.campaignId, "/end"), {
      method: "POST",
      body: JSON.stringify({
        contract_name: "live_end_request",
        contract_version: 2,
        operation_request: operation("live_end", await liveIdempotencyKey("end", end.sessionId, end.deviceId, end.operationId), await digest(input), workflowVersion),
        ...input,
      }),
    });
    return { readyForProposal: result.end_barrier?.ready_for_proposal === true, workflowVersion: result.workflow_version };
  },

  async readSession(campaignId: string, sessionId: string) {
    const result = await requestJson<LiveSessionView>(path(campaignId, ""));
    if (result.session_id !== sessionId) throw new Error("session_observe_mismatch");
    const acknowledgements = result.acknowledgements.map(({ device_id: deviceId, operation_id: operationId, payload_digest: payloadDigest, outcome }) => ({ deviceId, operationId, payloadDigest, outcome }));
    return {
      workflowVersion: result.workflow_version,
      controllerId: result.controller.controller_id,
      controllerEpoch: result.controller.epoch,
      endBarrier: result.end_barrier ? {
        deviceId: result.end_barrier.end_device_id,
        operationId: result.end_barrier.end_operation_id,
        readyForProposal: result.end_barrier.ready_for_proposal,
      } : null,
      acknowledgedOperationIds: acknowledgements.map(({ deviceId, operationId }) => ({ deviceId, operationId })),
      acknowledgements,
      captureOperationIds: result.events.map(({ device_id: deviceId, operation_id: operationId }) => ({ deviceId, operationId })),
      mode: result.mode,
    };
  },
};

export const httpLiveClient: LiveClient = {
  async start(campaignId, headRevision, controllerId) {
    await ensureCsrfToken();
    const retryStorageKey = `warden-live-start:${campaignId}:${controllerId}`;
    let identity: { session_id: string; request_id: string; idempotency_key: string };
    try {
      identity = JSON.parse(sessionStorage.getItem(retryStorageKey) ?? "null") ?? { session_id: browserId("session"), request_id: browserId("request"), idempotency_key: browserId("idem_start") };
      sessionStorage.setItem(retryStorageKey, JSON.stringify(identity));
    } catch {
      identity = { session_id: browserId("session"), request_id: browserId("request"), idempotency_key: browserId("idem_start") };
    }
    const input = { campaign_id: campaignId, session_id: identity.session_id, head_revision: headRevision, controller_id: controllerId };
    const body = {
      contract_name: "live_start_request",
      contract_version: 2,
      operation_request: {
        contract_name: "operation_request",
        contract_version: 2,
        request_id: identity.request_id,
        operation: "live_start",
        idempotency_key: identity.idempotency_key,
        payload_digest: await digest(input),
        expected_revision: null,
        expected_workflow_version: null,
      },
      ...input,
    };
    const result = await requestJson<LiveSessionView>(path(campaignId, ""), { method: "POST", body: JSON.stringify(body) });
    try { sessionStorage.removeItem(retryStorageKey); } catch { /* Storage can be disabled in private browsing. */ }
    return result;
  },
  observe: (campaignId) => requestJson<LiveSessionView>(path(campaignId, "")),
  async takeover(session, controllerId) {
    await ensureCsrfToken();
    const input = { campaign_id: session.campaign_id, session_id: session.session_id, controller_id: controllerId, controller_epoch: session.controller.epoch };
    const body = {
      contract_name: "live_takeover_request",
      contract_version: 2,
      operation_request: operation("live_takeover", browserId("idem_takeover"), await digest(input), session.workflow_version),
      ...input,
    };
    return requestJson<LiveSessionView>(path(session.campaign_id, "/takeover"), { method: "POST", body: JSON.stringify(body) });
  },
};
