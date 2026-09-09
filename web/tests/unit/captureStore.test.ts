import {
  CaptureConflictError,
  CaptureQueue,
  CaptureStorageError,
  MemoryCaptureStore,
  createIndexedDbCaptureStore,
  type CaptureInput,
  type CaptureSyncTransport,
} from "../../src/live/captureStore";

const input: CaptureInput = {
  campaignId: "campaign_alpha",
  sessionId: "session_alpha",
  baseRevision: "revision_12",
  controllerId: "controller_alpha",
  controllerEpoch: 1,
  workflowVersion: 1,
  captureType: "confirmed_fact",
  text: "The door opened.",
  recordId: "record-one",
  eventId: "event_alpha",
  operationId: "operation_alpha",
};

describe("durable live capture queue", () => {
  it("keeps one device identity and deterministic per-device order across tabs", async () => {
    const store = new MemoryCaptureStore();
    const [first, second] = await Promise.all([
      store.saveCapture(input),
      store.saveCapture({ ...input, eventId: "event_beta", operationId: "operation_beta", text: "The lights failed." }),
    ]);
    expect(first.deviceId).toBe(second.deviceId);
    expect(new Set([first.deviceOrder, second.deviceOrder])).toEqual(new Set([1, 2]));
    expect((await store.listCaptures("session_alpha")).map((capture) => capture.operationId)).toEqual(
      [first, second].sort((a, b) => a.deviceOrder - b.deviceOrder).map((capture) => capture.operationId),
    );
  });

  it("replays an exact operation without duplicating it and rejects changed content", async () => {
    const store = new MemoryCaptureStore();
    const saved = await store.saveCapture(input);
    const replay = await store.saveCapture(input);
    expect(replay).toEqual(saved);
    await expect(store.saveCapture({ ...input, text: "Changed after the fact." })).rejects.toBeInstanceOf(CaptureConflictError);
    expect(await store.listCaptures(input.sessionId)).toHaveLength(1);
  });

  it("keeps a capture pending through a temporary outage and syncs it on retry", async () => {
    const store = new MemoryCaptureStore();
    let attempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => {
        attempts += 1;
        if (attempts === 1) throw Object.assign(new Error("offline"), { retryable: true });
        return "accepted" as const;
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true })),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture(input);
    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Synced");
    expect(transport.sendCapture).toHaveBeenCalledTimes(2);
  });

  it("marks a digest conflict as needing attention without deleting the capture", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => "digest_conflict" as const),
      sendEnd: vi.fn(async () => ({ readyForProposal: true })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const result = await queue.sync(input.sessionId);
    expect(result.captures[0]).toEqual({ key: saved.key, state: "Needs attention" });
    expect((await store.listCaptures(input.sessionId))[0].text).toBe(input.text);
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("does not send the end intent until its exact local operation set is synced", async () => {
    const store = new MemoryCaptureStore();
    let captureAttempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => {
        captureAttempts += 1;
        if (captureAttempts === 1) throw Object.assign(new Error("offline"), { retryable: true });
        return "exact_replay" as const;
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const end = await queue.end({ ...input, operationId: undefined, requiredOperationIds: [{ deviceId: saved.deviceId, operationId: saved.operationId }] });
    expect((await queue.sync(input.sessionId)).end).toEqual({ key: end.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
    expect((await queue.sync(input.sessionId)).end).toEqual({ key: end.key, state: "Synced" });
    expect(transport.sendEnd).toHaveBeenCalledTimes(1);
  });

  it("reports unavailable browser storage instead of claiming local success", async () => {
    const store = createIndexedDbCaptureStore();
    await expect(store.getDeviceId()).rejects.toBeInstanceOf(CaptureStorageError);
  });
});
