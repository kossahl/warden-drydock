import { digest } from "./digest";
import { browserId, requestJson } from "./client";
import type { LiveCaptureResult, LiveSessionView, OperationRequest } from "../contracts/v2";
import type { CaptureOutcome, CaptureSyncTransport, StoredCapture, StoredEndIntent } from "../live/captureStore";

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

export const httpCaptureTransport: CaptureSyncTransport = {
  async sendCapture(capture: StoredCapture): Promise<CaptureOutcome> {
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
    return (await requestJson<LiveCaptureResult>(path(capture.campaignId, "/captures"), {
      method: "POST",
      body: JSON.stringify({
        contract_name: "live_capture_request",
        contract_version: 2,
        operation_request: operation("live_capture", capture.operationId, await digest(input), capture.workflowVersion),
        ...input,
      }),
    })).outcome;
  },

  async sendEnd(end: StoredEndIntent): Promise<{ readyForProposal: boolean }> {
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
        operation_request: operation("live_end", end.operationId, await digest(input), end.workflowVersion),
        ...input,
      }),
    });
    return { readyForProposal: result.end_barrier?.ready_for_proposal === true };
  },
};
