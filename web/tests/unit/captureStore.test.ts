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

  it("rejects new captures after the end intent is persisted", async () => {
    const store = new MemoryCaptureStore();
    await store.saveEnd({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    await expect(store.saveCapture({ ...input, eventId: "event_beta", operationId: "operation_beta", text: "The lights failed." })).rejects.toThrow("session_end_immutable");
  });

  it("keeps a capture pending through a temporary outage and syncs it on retry", async () => {
    const store = new MemoryCaptureStore();
    let attempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => {
        attempts += 1;
        if (attempts === 1) throw Object.assign(new Error("offline"), { retryable: true });
        return { outcome: "accepted" as const, workflowVersion: 2 };
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 2 })),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture(input);
    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Synced");
    expect(transport.sendCapture).toHaveBeenCalledTimes(2);
  });

  it("stops draining later captures after a retryable failure", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => { throw Object.assign(new Error("offline"), { retryable: true }); }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 2 })),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture(input);
    await queue.capture({ ...input, eventId: "event_beta", operationId: "operation_beta", text: "The lights failed." });

    const result = await queue.sync(input.sessionId);

    expect(transport.sendCapture).toHaveBeenCalledTimes(1);
    expect(result.captures.every(({ state }) => state === "Saved on device")).toBe(true);
  });

  it("keeps a stale workflow race retryable for the next sync", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    let attempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => {
        attempts += 1;
        if (attempts === 1) throw Object.assign(new Error("stale workflow"), { status: 409, code: "stale_workflow_version" });
        return { outcome: "accepted" as const, workflowVersion: 3 };
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => ({ workflowVersion: ++reads, acknowledgedOperationIds: [] })),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture(input);

    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Synced");
    expect(transport.sendCapture).toHaveBeenNthCalledWith(2, expect.anything(), 2);
  });

  it("reconciles a committed capture when its response was lost", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    let attempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => {
        attempts += 1;
        throw Object.assign(new Error("response lost"), { retryable: true });
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => {
        reads += 1;
        const capture = (await store.listCaptures(input.sessionId))[0];
        return {
          workflowVersion: reads,
          acknowledgedOperationIds: capture && reads > 1 ? [{ deviceId: capture.deviceId, operationId: capture.operationId }] : [],
          acknowledgements: capture && reads > 1 ? [{ deviceId: capture.deviceId, operationId: capture.operationId, payloadDigest: capture.payloadDigest, outcome: "accepted" as const }] : [],
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture(input);

    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId)).captures[0].state).toBe("Synced");
    expect(attempts).toBe(1);
  });

  it("marks a digest conflict as needing attention without deleting the capture", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "digest_conflict" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 2 })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const result = await queue.sync(input.sessionId);
    expect(result.captures[0]).toEqual({ key: saved.key, state: "Needs attention" });
    expect((await store.listCaptures(input.sessionId))[0].text).toBe(input.text);
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("does not let a conflicting local capture satisfy the end barrier", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "digest_conflict" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 2 })),
      readSession: vi.fn(async () => ({ workflowVersion: 1, acknowledgedOperationIds: [
        { deviceId: await store.getDeviceId(), operationId: "operation_alpha" },
      ] })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [{ deviceId: saved.deviceId, operationId: saved.operationId }] });

    const result = await queue.sync(input.sessionId);

    expect(result.captures).toEqual([{ key: saved.key, state: "Needs attention" }]);
    expect(result.end).toEqual({ key: end.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("surfaces a permanent session observation failure on queued records", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => { throw new Error("session_observe_mismatch"); }),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [{ deviceId: saved.deviceId, operationId: saved.operationId }] });

    const result = await queue.sync(input.sessionId);

    expect(result.captures).toEqual([{ key: saved.key, state: "Needs attention" }]);
    expect(result.end).toEqual({ key: end.key, state: "Needs attention" });
  });

  it("surfaces a session that ended in another tab", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => ({ workflowVersion: 2, acknowledgedOperationIds: [], mode: "ended_review_pending" as const })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [{ deviceId: saved.deviceId, operationId: saved.operationId }] });

    const result = await queue.sync(input.sessionId);

    expect(result.captures).toEqual([{ key: saved.key, state: "Needs attention" }]);
    expect(result.end).toEqual({ key: end.key, state: "Needs attention" });
    expect(transport.sendCapture).not.toHaveBeenCalled();
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("rejects an end watermark that hides a server capture", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => ({ workflowVersion: 2, acknowledgedOperationIds: [
        { deviceId: "device_alpha", operationId: "operation_alpha" },
        { deviceId: "device_remote", operationId: "operation_remote" },
      ], captureOperationIds: [
        { deviceId: "device_alpha", operationId: "operation_alpha" },
        { deviceId: "device_remote", operationId: "operation_remote" },
      ] })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [{ deviceId: saved.deviceId, operationId: saved.operationId }] });

    const result = await queue.sync(input.sessionId);

    expect(result.end).toEqual({ key: end.key, state: "Needs attention" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("does not send the end intent until its exact local operation set is synced", async () => {
    const store = new MemoryCaptureStore();
    let captureAttempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => {
        captureAttempts += 1;
        if (captureAttempts === 1) throw Object.assign(new Error("offline"), { retryable: true });
        return { outcome: "exact_replay" as const, workflowVersion: 2 };
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const end = await queue.end({ ...input, operationId: undefined, requiredOperationIds: [{ deviceId: saved.deviceId, operationId: saved.operationId }] });
    expect((await queue.sync(input.sessionId)).end).toEqual({ key: end.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
    expect((await queue.sync(input.sessionId)).end).toEqual({ key: end.key, state: "Synced" });
    expect(transport.sendEnd).toHaveBeenCalledTimes(1);
  });

  it("carries the returned workflow version through queued captures and the end barrier", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async (_capture, workflowVersion) => ({
        outcome: "accepted" as const,
        workflowVersion: workflowVersion + 1,
      })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 4 })),
    };
    const queue = new CaptureQueue(store, transport);
    const first = await queue.capture(input);
    const second = await queue.capture({ ...input, eventId: "event_beta", operationId: "operation_beta", text: "The lights failed." });
    await queue.end({ ...input, operationId: undefined, requiredOperationIds: [
      { deviceId: first.deviceId, operationId: first.operationId },
      { deviceId: second.deviceId, operationId: second.operationId },
    ] });

    await queue.sync(input.sessionId);

    expect(transport.sendCapture).toHaveBeenNthCalledWith(1, expect.objectContaining({ operationId: "operation_alpha" }), 1);
    expect(transport.sendCapture).toHaveBeenNthCalledWith(2, expect.objectContaining({ operationId: "operation_beta" }), 2);
    expect(transport.sendEnd).toHaveBeenCalledWith(expect.anything(), 3);
  });

  it("refreshes the server workflow version before a later sync run", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async (_capture, workflowVersion) => ({ outcome: "accepted" as const, workflowVersion: workflowVersion + 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 8 })),
      readSession: vi.fn(async () => ({ workflowVersion: 7, acknowledgedOperationIds: [] })),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture({ ...input, workflowVersion: 1 });

    await queue.sync(input.sessionId);

    expect(transport.sendCapture).toHaveBeenCalledWith(expect.anything(), 7);
  });

  it("allows required acknowledgements from other devices through the end barrier", async () => {
    const store = new MemoryCaptureStore("device_alpha");
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => ({ workflowVersion: 2, acknowledgedOperationIds: [
        { deviceId: "device_remote", operationId: "operation_remote" },
      ] })),
    };
    const queue = new CaptureQueue(store, transport);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [
      { deviceId: "device_remote", operationId: "operation_remote" },
    ] });

    const result = await queue.sync(input.sessionId);

    expect(result.end).toEqual({ key: end.key, state: "Synced" });
    expect(transport.sendEnd).toHaveBeenCalledTimes(1);
  });

  it("waits for a remote required acknowledgement before sending the end barrier", async () => {
    const store = new MemoryCaptureStore("device_alpha");
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 3 })),
      readSession: vi.fn(async () => ({ workflowVersion: 2, acknowledgedOperationIds: [] })),
    };
    const queue = new CaptureQueue(store, transport);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [
      { deviceId: "device_remote", operationId: "operation_remote" },
    ] });

    const result = await queue.sync(input.sessionId);

    expect(result.end).toEqual({ key: end.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("reports unavailable browser storage instead of claiming local success", async () => {
    const store = createIndexedDbCaptureStore();
    await expect(store.getDeviceId()).rejects.toBeInstanceOf(CaptureStorageError);
  });
});
