import {
  CaptureConflictError,
  CaptureQueue,
  CaptureStorageError,
  MemoryCaptureStore,
  captureDigest,
  createIndexedDbCaptureStore,
  endDigest,
  type CaptureInput,
  type CaptureSyncTransport,
  type StoredCapture,
} from "../../src/live/captureStore";
import { ApiError } from "../../src/api/client";

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
    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Synced");
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

    const result = await queue.sync(input.sessionId, input.controllerId);

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

    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Synced");
    expect(transport.sendCapture).toHaveBeenNthCalledWith(2, expect.anything(), 2);
  });

  it("rebinds an unacknowledged stale-controller capture after observing the active controller", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    const attempts: Array<{ capture: StoredCapture; workflowVersion: number }> = [];
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async (capture, workflowVersion) => {
        attempts.push({ capture, workflowVersion });
        if (attempts.length === 1) throw new ApiError(409, "stale_controller_epoch");
        return { outcome: "accepted" as const, workflowVersion: workflowVersion + 1 };
      }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 1 })),
      readSession: vi.fn(async () => {
        reads += 1;
        return {
          workflowVersion: reads === 1 ? 2 : 8,
          controllerId: reads === 1 ? "controller_alpha" : "controller_beta",
          controllerEpoch: reads === 1 ? 1 : 2,
          acknowledgedOperationIds: [],
          acknowledgements: [],
          mode: "active" as const,
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);

    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0]).toEqual({ key: saved.key, state: "Saved on device" });
    expect((await store.listCaptures(input.sessionId))[0].lastError).toBe("stale_controller_epoch");
    expect((await queue.sync(input.sessionId, "controller_beta")).captures[0].state).toBe("Synced");

    expect(attempts).toHaveLength(2);
    expect(attempts[1]).toMatchObject({
      workflowVersion: 8,
      capture: {
        campaignId: saved.campaignId,
        sessionId: saved.sessionId,
        baseRevision: saved.baseRevision,
        deviceId: saved.deviceId,
        deviceOrder: saved.deviceOrder,
        eventId: saved.eventId,
        operationId: saved.operationId,
        captureType: saved.captureType,
        text: saved.text,
        recordId: saved.recordId,
        controllerId: "controller_beta",
        controllerEpoch: 2,
        workflowVersion: 8,
      },
    });
    expect(attempts[1].capture.attemptedPayloadDigest).toBe(await captureDigest({ ...attempts[1].capture, workflowVersion: 8 }));
  });

  it("does not rebind a capture to a controller owned by another tab", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async (_capture, workflowVersion) => ({ outcome: "accepted" as const, workflowVersion: workflowVersion + 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 1 })),
      readSession: vi.fn(async () => ({
        workflowVersion: 8,
        controllerId: "controller_beta",
        controllerEpoch: 2,
        acknowledgedOperationIds: [],
        acknowledgements: [],
        mode: "active" as const,
      })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);

    const observerSync = await queue.sync(input.sessionId, "controller_alpha");

    expect(observerSync.captures).toEqual([{ key: saved.key, state: "Saved on device" }]);
    expect(transport.sendCapture).not.toHaveBeenCalled();
    expect(await store.listCaptures(input.sessionId)).toEqual([expect.objectContaining({
      controllerId: "controller_alpha",
      controllerEpoch: 1,
      payloadDigest: saved.payloadDigest,
      lastError: "stale_controller",
    })]);

    expect((await queue.sync(input.sessionId, "controller_beta")).captures).toEqual([{ key: saved.key, state: "Synced" }]);
    expect(transport.sendCapture).toHaveBeenCalledWith(expect.objectContaining({ controllerId: "controller_beta", controllerEpoch: 2 }), 8);
  });

  it("reconciles an exact acknowledgement before rebinding after takeover", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => { throw Object.assign(new Error("response lost"), { retryable: true }); }),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 1 })),
      readSession: vi.fn(async () => {
        reads += 1;
        const capture = (await store.listCaptures(input.sessionId))[0];
        const current = reads === 1;
        return {
          workflowVersion: 9,
          controllerId: current ? "controller_alpha" : "controller_beta",
          controllerEpoch: current ? 1 : 2,
          acknowledgedOperationIds: capture && reads > 1 ? [{ deviceId: capture.deviceId, operationId: capture.operationId }] : [],
          acknowledgements: capture && reads > 1 ? [{
            deviceId: capture.deviceId,
            operationId: capture.operationId,
            payloadDigest: capture.attemptedPayloadDigest!,
            outcome: "accepted" as const,
          }] : [],
          mode: "active" as const,
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);

    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0]).toEqual({ key: saved.key, state: "Synced" });
    expect(transport.sendCapture).toHaveBeenCalledTimes(1);
    expect((await store.listCaptures(input.sessionId))[0]).toMatchObject({ controllerId: "controller_alpha", controllerEpoch: 1 });
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
          workflowVersion: reads === 1 ? 2 : 3,
          acknowledgedOperationIds: capture && reads > 1 ? [{ deviceId: capture.deviceId, operationId: capture.operationId }] : [],
          acknowledgements: capture && reads > 1 ? [{ deviceId: capture.deviceId, operationId: capture.operationId, payloadDigest: await captureDigest({ ...capture, workflowVersion: 2 }), outcome: "accepted" as const }] : [],
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    await queue.capture(input);

    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Saved on device");
    expect((await queue.sync(input.sessionId, input.controllerId)).captures[0].state).toBe("Synced");
    expect(attempts).toBe(1);
  });

  it("reconciles an end intent when its response was lost", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    let attempts = 0;
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd: vi.fn(async () => {
        attempts += 1;
        throw Object.assign(new Error("response lost"), { retryable: true });
      }),
      readSession: vi.fn(async () => {
        reads += 1;
        const end = await store.getEnd(input.sessionId);
        const acknowledged = end && reads > 2 ? [{ deviceId: end.deviceId, operationId: end.operationId }] : [];
        const acknowledgements = end && reads > 2 ? [{ deviceId: end.deviceId, operationId: end.operationId, payloadDigest: await endDigest({ ...end, workflowVersion: 2 }), outcome: "accepted" as const }] : [];
        return { workflowVersion: 2, acknowledgedOperationIds: acknowledged, acknowledgements, mode: reads > 2 ? "ended_review_pending" as const : "active" as const };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Saved on device" });
    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Synced" });
    expect(attempts).toBe(1);
  });

  it("rebinds an unacknowledged stale-controller end intent after observing the active controller", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    const attempts: Array<{ end: Awaited<ReturnType<MemoryCaptureStore["saveEnd"]>>; workflowVersion: number }> = [];
    const requiredOperationIds = [{ deviceId: "device_remote", operationId: "operation_remote" }];
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async (end, workflowVersion) => {
        attempts.push({ end, workflowVersion });
        if (attempts.length === 1) throw new ApiError(409, "stale_controller_epoch");
        return { readyForProposal: true, workflowVersion: workflowVersion + 1 };
      }),
      readSession: vi.fn(async () => {
        reads += 1;
        const current = reads <= 2;
        return {
          workflowVersion: current ? 2 : 8,
          controllerId: current ? "controller_alpha" : "controller_beta",
          controllerEpoch: current ? 1 : 2,
          acknowledgedOperationIds: requiredOperationIds,
          acknowledgements: [],
          mode: "active" as const,
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds });

    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: saved.key, state: "Saved on device" });
    expect((await store.getEnd(input.sessionId))?.lastError).toBe("stale_controller_epoch");
    expect((await queue.sync(input.sessionId, "controller_beta")).end).toEqual({ key: saved.key, state: "Synced" });

    expect(attempts).toHaveLength(2);
    expect(attempts[1]).toMatchObject({
      workflowVersion: 8,
      end: {
        campaignId: saved.campaignId,
        sessionId: saved.sessionId,
        baseRevision: saved.baseRevision,
        deviceId: saved.deviceId,
        operationId: saved.operationId,
        requiredOperationIds,
        controllerId: "controller_beta",
        controllerEpoch: 2,
        workflowVersion: 8,
      },
    });
    expect(attempts[1].end.attemptedPayloadDigest).toBe(await endDigest({ ...attempts[1].end, workflowVersion: 8 }));
  });

  it("does not rebind an end intent to a controller owned by another tab", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async (_end, workflowVersion) => ({ readyForProposal: true, workflowVersion: workflowVersion + 1 })),
      readSession: vi.fn(async () => ({
        workflowVersion: 8,
        controllerId: "controller_beta",
        controllerEpoch: 2,
        acknowledgedOperationIds: [],
        acknowledgements: [],
        mode: "active" as const,
      })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    const observerSync = await queue.sync(input.sessionId, "controller_alpha");

    expect(observerSync.end).toEqual({ key: saved.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
    expect(await store.getEnd(input.sessionId)).toEqual(expect.objectContaining({
      controllerId: "controller_alpha",
      controllerEpoch: 1,
      payloadDigest: saved.payloadDigest,
      lastError: "stale_controller",
    }));

    expect((await queue.sync(input.sessionId, "controller_beta")).end).toEqual({ key: saved.key, state: "Synced" });
    expect(transport.sendEnd).toHaveBeenCalledWith(expect.objectContaining({ controllerId: "controller_beta", controllerEpoch: 2 }), 8);
  });

  it("does not repair an end-intent payload from an observer tab", async () => {
    const store = new MemoryCaptureStore();
    const remoteCapture = { deviceId: "device_remote", operationId: "operation_remote" };
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 9 })),
      readSession: vi.fn(async () => ({
        workflowVersion: 8,
        controllerId: "controller_beta",
        controllerEpoch: 2,
        acknowledgedOperationIds: [remoteCapture],
        acknowledgements: [{ ...remoteCapture, payloadDigest: "4".repeat(64), outcome: "accepted" as const }],
        captureOperationIds: [remoteCapture],
        mode: "active" as const,
      })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    const result = await queue.sync(input.sessionId, "controller_alpha");

    expect(result.end).toEqual({ key: saved.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
    expect(await store.getEnd(input.sessionId)).toEqual(expect.objectContaining({
      requiredOperationIds: [],
      payloadDigest: saved.payloadDigest,
      lastError: "stale_controller",
    }));
  });

  it("reconciles an exact end acknowledgement before rebinding after takeover", async () => {
    const store = new MemoryCaptureStore();
    let reads = 0;
    const sentEnds: Awaited<ReturnType<MemoryCaptureStore["saveEnd"]>>[] = [];
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async (end) => {
        sentEnds.push(end);
        throw Object.assign(new Error("response lost"), { retryable: true });
      }),
      readSession: vi.fn(async () => {
        reads += 1;
        const end = await store.getEnd(input.sessionId);
        const includeReceipt = reads > 2 && Boolean(end?.attemptedPayloadDigest);
        const current = reads <= 2;
        return {
          workflowVersion: 9,
          controllerId: current ? "controller_alpha" : "controller_beta",
          controllerEpoch: current ? 1 : 2,
          acknowledgedOperationIds: [],
          acknowledgements: [],
          endBarrier: includeReceipt ? { deviceId: end!.deviceId, operationId: end!.operationId, readyForProposal: true } : null,
          mode: includeReceipt ? "ended_review_pending" as const : "active" as const,
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: saved.key, state: "Saved on device" });
    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: saved.key, state: "Synced" });
    expect(sentEnds).toHaveLength(1);
    expect(sentEnds[0]).toMatchObject({ controllerId: "controller_alpha", controllerEpoch: 1 });
  });

  it("does not mark an acknowledged end intent synced when its barrier is not ready", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: false, workflowVersion: 2 })),
      readSession: vi.fn(async () => {
        const end = await store.getEnd(input.sessionId);
        return {
          workflowVersion: 2,
          controllerId: "controller_alpha",
          controllerEpoch: 1,
          acknowledgedOperationIds: [],
          acknowledgements: [],
          endBarrier: end ? { deviceId: end.deviceId, operationId: end.operationId, readyForProposal: false } : null,
          mode: "ended_review_pending" as const,
        };
      }),
    };
    const queue = new CaptureQueue(store, transport);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Needs attention" });
    expect((await store.getEnd(input.sessionId))?.lastError).toBe("live_session_ended");
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("marks a digest conflict as needing attention without deleting the capture", async () => {
    const store = new MemoryCaptureStore();
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "digest_conflict" as const, workflowVersion: 1 })),
      sendEnd: vi.fn(async () => ({ readyForProposal: true, workflowVersion: 2 })),
    };
    const queue = new CaptureQueue(store, transport);
    const saved = await queue.capture(input);
    const result = await queue.sync(input.sessionId, input.controllerId);
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

    const result = await queue.sync(input.sessionId, input.controllerId);

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

    const result = await queue.sync(input.sessionId, input.controllerId);

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

    const result = await queue.sync(input.sessionId, input.controllerId);

    expect(result.captures).toEqual([{ key: saved.key, state: "Needs attention" }]);
    expect(result.end).toEqual({ key: end.key, state: "Needs attention" });
    expect(transport.sendCapture).not.toHaveBeenCalled();
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("repairs an end watermark to include the exact acknowledged server capture set", async () => {
    const store = new MemoryCaptureStore("device_alpha");
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

    const result = await queue.sync(input.sessionId, input.controllerId);

    const required = [
      { deviceId: saved.deviceId, operationId: saved.operationId },
      { deviceId: "device_remote", operationId: "operation_remote" },
    ];
    expect(result.captures).toEqual([{ key: saved.key, state: "Synced" }]);
    expect(result.end).toEqual({ key: end.key, state: "Synced" });
    expect((await store.getEnd(input.sessionId))?.requiredOperationIds).toEqual(required);
    expect(transport.sendEnd).toHaveBeenCalledTimes(1);
    expect(transport.sendEnd).toHaveBeenCalledWith(expect.objectContaining({ operationId: end.operationId, requiredOperationIds: required }), 2);
  });

  it("repairs a stale end barrier from the latest active session receipts and retries the same intent", async () => {
    const store = new MemoryCaptureStore("device_alpha");
    const remote = { deviceId: "device_remote", operationId: "operation_remote" };
    let reads = 0;
    let acceptedBody: StoredCapture | null = null;
    const sendEnd = vi.fn(async (end: Awaited<ReturnType<MemoryCaptureStore["saveEnd"]>>) => {
      acceptedBody = end as unknown as StoredCapture;
      return { readyForProposal: true, workflowVersion: 3 };
    });
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd,
      readSession: vi.fn(async () => ({
        workflowVersion: ++reads,
        acknowledgedOperationIds: reads >= 2 ? [remote] : [],
        captureOperationIds: reads >= 2 ? [remote] : [],
        acknowledgements: reads >= 2 ? [{ ...remote, payloadDigest: "a".repeat(64), outcome: "accepted" as const }] : [],
        mode: "active" as const,
      })),
    };
    const queue = new CaptureQueue(store, transport);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    const result = await queue.sync(input.sessionId, input.controllerId);
    const repaired = await store.getEnd(input.sessionId);

    expect(result.end).toEqual({ key: end.key, state: "Synced" });
    expect(repaired?.requiredOperationIds).toEqual([remote]);
    expect(sendEnd).toHaveBeenCalledTimes(1);
    expect(sendEnd.mock.calls[0][0].operationId).toBe(end.operationId);
    expect(sendEnd.mock.calls[0][0].requiredOperationIds).toEqual([remote]);
    expect(acceptedBody).not.toBeNull();
  });

  it("recovers live_unaccepted_barrier after a new server capture appears", async () => {
    const store = new MemoryCaptureStore("device_alpha");
    const remote = { deviceId: "device_remote", operationId: "operation_remote" };
    let remoteCaptureAccepted = false;
    const sendEnd = vi.fn(async (_end: Awaited<ReturnType<MemoryCaptureStore["saveEnd"]>>, workflowVersion: number) => {
      if (!remoteCaptureAccepted) {
        remoteCaptureAccepted = true;
        throw new ApiError(409, "live_unaccepted_barrier");
      }
      return { readyForProposal: true, workflowVersion: workflowVersion + 1 };
    });
    const transport: CaptureSyncTransport = {
      sendCapture: vi.fn(async () => ({ outcome: "accepted" as const, workflowVersion: 2 })),
      sendEnd,
      readSession: vi.fn(async () => ({
        workflowVersion: remoteCaptureAccepted ? 3 : 1,
        acknowledgedOperationIds: remoteCaptureAccepted ? [remote] : [],
        captureOperationIds: remoteCaptureAccepted ? [remote] : [],
        acknowledgements: remoteCaptureAccepted ? [{ ...remote, payloadDigest: "b".repeat(64), outcome: "accepted" as const }] : [],
        mode: "active" as const,
      })),
    };
    const queue = new CaptureQueue(store, transport);
    const end = await queue.end({ ...input, operationId: "operation_end", requiredOperationIds: [] });

    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Needs attention" });
    expect((await store.getEnd(input.sessionId))?.lastError).toBe("live_unaccepted_barrier");
    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Synced" });

    const retried = sendEnd.mock.calls[1][0];
    expect(sendEnd).toHaveBeenCalledTimes(2);
    expect(retried.operationId).toBe(end.operationId);
    expect(retried.requiredOperationIds).toEqual([remote]);
    expect((await store.getEnd(input.sessionId))?.requiredOperationIds).toEqual([remote]);
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
    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
    expect((await queue.sync(input.sessionId, input.controllerId)).end).toEqual({ key: end.key, state: "Synced" });
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

    await queue.sync(input.sessionId, input.controllerId);

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

    await queue.sync(input.sessionId, input.controllerId);

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

    const result = await queue.sync(input.sessionId, input.controllerId);

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

    const result = await queue.sync(input.sessionId, input.controllerId);

    expect(result.end).toEqual({ key: end.key, state: "Saved on device" });
    expect(transport.sendEnd).not.toHaveBeenCalled();
  });

  it("reports unavailable browser storage instead of claiming local success", async () => {
    const store = createIndexedDbCaptureStore();
    await expect(store.getDeviceId()).rejects.toBeInstanceOf(CaptureStorageError);
  });
});
