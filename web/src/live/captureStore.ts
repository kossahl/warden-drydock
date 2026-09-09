import { digest } from "../api/digest";
import type { Digest, LiveSessionMode, PublicId, SaveSyncState } from "../contracts/v2";

export type CaptureType = "confirmed_fact" | "unresolved_question";
export type CaptureState = SaveSyncState;

export interface ReceiptIdentity {
  deviceId: PublicId;
  operationId: PublicId;
}

export interface CaptureInput {
  campaignId: PublicId;
  sessionId: PublicId;
  baseRevision: PublicId;
  controllerId: PublicId;
  controllerEpoch: number;
  workflowVersion: number;
  captureType: CaptureType;
  text: string;
  recordId?: string | null;
  eventId?: PublicId;
  operationId?: PublicId;
}

export interface StoredCapture extends CaptureInput {
  key: string;
  deviceId: PublicId;
  deviceOrder: number;
  eventId: PublicId;
  operationId: PublicId;
  recordId: string | null;
  payloadDigest: Digest;
  attemptedWorkflowVersion: number | null;
  attemptedPayloadDigest: Digest | null;
  state: CaptureState;
  lastError: string | null;
}

export interface EndInput {
  campaignId: PublicId;
  sessionId: PublicId;
  baseRevision: PublicId;
  controllerId: PublicId;
  controllerEpoch: number;
  workflowVersion: number;
  requiredOperationIds?: readonly ReceiptIdentity[];
  operationId?: PublicId;
}

export interface StoredEndIntent extends EndInput {
  key: string;
  deviceId: PublicId;
  operationId: PublicId;
  requiredOperationIds: readonly ReceiptIdentity[];
  payloadDigest: Digest;
  attemptedWorkflowVersion: number | null;
  attemptedPayloadDigest: Digest | null;
  state: CaptureState;
  lastError: string | null;
}

export type CaptureOutcome = "accepted" | "exact_replay" | "digest_conflict";

export interface CaptureSyncResponse {
  outcome: CaptureOutcome;
  workflowVersion: number;
}

export interface EndSyncResponse {
  readyForProposal: boolean;
  workflowVersion: number;
}

export interface SessionSyncResponse {
  workflowVersion: number;
  acknowledgedOperationIds: ReadonlyArray<ReceiptIdentity>;
  acknowledgements?: ReadonlyArray<SessionAcknowledgement>;
  captureOperationIds?: ReadonlyArray<ReceiptIdentity>;
  mode?: LiveSessionMode;
}

export interface SessionAcknowledgement extends ReceiptIdentity {
  payloadDigest: Digest;
  outcome: CaptureOutcome;
}

export interface CaptureSyncTransport {
  sendCapture(capture: StoredCapture, workflowVersion: number): Promise<CaptureSyncResponse>;
  sendEnd(end: StoredEndIntent, workflowVersion: number): Promise<EndSyncResponse>;
  readSession?(campaignId: PublicId, sessionId: PublicId): Promise<SessionSyncResponse>;
}

export interface CaptureStore {
  getDeviceId(): Promise<PublicId>;
  saveCapture(input: CaptureInput): Promise<StoredCapture>;
  listCaptures(sessionId: PublicId): Promise<StoredCapture[]>;
  updateCapture(key: string, state: CaptureState, lastError?: string | null): Promise<StoredCapture>;
  updateCaptureBinding(key: string, workflowVersion: number, payloadDigest: Digest): Promise<StoredCapture>;
  saveEnd(input: EndInput): Promise<StoredEndIntent>;
  getEnd(sessionId: PublicId): Promise<StoredEndIntent | null>;
  updateEnd(key: string, state: CaptureState, lastError?: string | null): Promise<StoredEndIntent>;
  updateEndBinding(key: string, workflowVersion: number, payloadDigest: Digest): Promise<StoredEndIntent>;
}

export interface CaptureSyncResult {
  captures: ReadonlyArray<{ key: string; state: CaptureState }>;
  end: { key: string; state: CaptureState } | null;
}

const publicIdPattern = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;
const domainIdPattern = /^[a-z0-9][a-z0-9-]*$/;
let fallbackId = 0;

function id(prefix: string): PublicId {
  fallbackId += 1;
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "") ?? `${Date.now().toString(36)}${fallbackId.toString(36)}`;
  return `${prefix}_${random}`;
}

function assertPublicId(value: string, field: string): void {
  if (value.length < 3 || value.length > 80 || !publicIdPattern.test(value)) throw new Error(`${field}_invalid`);
}

function assertContext(input: { campaignId: string; sessionId: string; baseRevision: string; controllerId: string; controllerEpoch: number; workflowVersion: number }): void {
  assertPublicId(input.campaignId, "campaign_id");
  assertPublicId(input.sessionId, "session_id");
  assertPublicId(input.baseRevision, "base_revision");
  assertPublicId(input.controllerId, "controller_id");
  if (!Number.isInteger(input.controllerEpoch) || input.controllerEpoch < 1) throw new Error("controller_epoch_invalid");
  if (!Number.isInteger(input.workflowVersion) || input.workflowVersion < 1) throw new Error("workflow_version_invalid");
}

function assertCaptureInput(input: CaptureInput): void {
  assertContext(input);
  if (input.captureType !== "confirmed_fact" && input.captureType !== "unresolved_question") throw new Error("capture_type_invalid");
  if (!input.text) throw new Error("capture_text_required");
  if (input.text.length > 100000) throw new Error("capture_text_too_long");
  if (input.recordId !== undefined && input.recordId !== null && !domainIdPattern.test(input.recordId)) throw new Error("record_id_invalid");
  if (input.eventId) assertPublicId(input.eventId, "event_id");
  if (input.operationId) assertPublicId(input.operationId, "operation_id");
}

function assertReceipt(value: ReceiptIdentity): void {
  assertPublicId(value.deviceId, "device_id");
  assertPublicId(value.operationId, "operation_id");
}

function sortedReceipts(values: readonly ReceiptIdentity[]): ReceiptIdentity[] {
  const result = values.map((value) => ({ ...value }));
  result.forEach(assertReceipt);
  const keys = result.map((value) => `${value.deviceId}\u0000${value.operationId}`);
  if (new Set(keys).size !== keys.length) throw new Error("required_operation_ids_duplicate");
  return result.sort((a, b) => `${a.deviceId}\u0000${a.operationId}`.localeCompare(`${b.deviceId}\u0000${b.operationId}`));
}

function captureKey(sessionId: string, deviceId: string, operationId: string): string {
  return `${sessionId}\u0000${deviceId}\u0000${operationId}`;
}

function endKey(sessionId: string, deviceId: string, operationId: string): string {
  return `${sessionId}\u0000${deviceId}\u0000${operationId}`;
}

export class CaptureConflictError extends Error {
  public readonly code = "idempotency_digest_conflict";
  public constructor(message = "capture operation identity is already bound to different content") {
    super(message);
  }
}

export class CaptureStorageError extends Error {
  public readonly code = "capture_storage_unavailable";
  public constructor(message = "browser storage is unavailable") {
    super(message);
  }
}

export function isRetryableCaptureError(error: unknown): boolean {
  if (typeof error === "object" && error !== null && "retryable" in error && typeof error.retryable === "boolean") return error.retryable;
  if (typeof error === "object" && error !== null && "code" in error && (error.code === "stale_workflow_version" || error.code === "stale_workflow")) return true;
  if (error instanceof Error && error.message === "session_observe_mismatch") return false;
  if (typeof error === "object" && error !== null && "status" in error && typeof error.status === "number") return error.status >= 500;
  return true;
}

export async function captureDigest(input: StoredCapture | Omit<StoredCapture, "key" | "payloadDigest" | "attemptedWorkflowVersion" | "attemptedPayloadDigest" | "state" | "lastError">): Promise<Digest> {
  const binding: Record<string, unknown> = {
    campaign_id: input.campaignId,
    base_revision: input.baseRevision,
    session_id: input.sessionId,
    controller_id: input.controllerId,
    controller_epoch: input.controllerEpoch,
    workflow_version: input.workflowVersion,
    event_type: input.captureType,
    event_id: input.eventId,
    device_id: input.deviceId,
    operation_id: input.operationId,
    device_order: input.deviceOrder,
    text: input.text,
  };
  if (input.recordId !== null && input.recordId !== undefined) binding.record_id = input.recordId;
  return digest(binding);
}

export async function endDigest(input: StoredEndIntent | Omit<StoredEndIntent, "key" | "payloadDigest" | "attemptedWorkflowVersion" | "attemptedPayloadDigest" | "state" | "lastError">): Promise<Digest> {
  return digest({
    campaign_id: input.campaignId,
    base_revision: input.baseRevision,
    session_id: input.sessionId,
    controller_id: input.controllerId,
    controller_epoch: input.controllerEpoch,
    workflow_version: input.workflowVersion,
    event_type: "end_intent",
    event_id: null,
    device_id: input.deviceId,
    operation_id: input.operationId,
    device_order: null,
    text: "",
    required_operation_ids: input.requiredOperationIds
      .map(({ deviceId, operationId }) => [deviceId, operationId])
      .sort(([a, aOperation], [b, bOperation]) => `${a}\u0000${aOperation}`.localeCompare(`${b}\u0000${bOperation}`)),
  });
}

function copyCapture(value: StoredCapture): StoredCapture {
  return { ...value };
}

function copyEnd(value: StoredEndIntent): StoredEndIntent {
  return { ...value, requiredOperationIds: value.requiredOperationIds.map((item) => ({ ...item })) };
}

export class MemoryCaptureStore implements CaptureStore {
  private readonly captures = new Map<string, StoredCapture>();
  private readonly ends = new Map<string, StoredEndIntent>();
  private deviceId?: PublicId;
  private deviceOrder = 0;

  public constructor(deviceId?: PublicId) {
    if (deviceId) assertPublicId(deviceId, "device_id");
    this.deviceId = deviceId;
  }

  public async getDeviceId(): Promise<PublicId> {
    this.deviceId ??= id("device");
    return this.deviceId;
  }

  public async saveCapture(input: CaptureInput): Promise<StoredCapture> {
    assertCaptureInput(input);
    const deviceId = await this.getDeviceId();
    const operationId = input.operationId ?? id("operation");
    const eventId = input.eventId ?? id("event");
    const key = captureKey(input.sessionId, deviceId, operationId);
    const existing = this.captures.get(key);
    if (existing) {
      const candidate = await captureDigest({ ...input, deviceId, operationId, eventId: input.eventId ?? existing.eventId, deviceOrder: existing.deviceOrder, recordId: input.recordId ?? null });
      if (candidate !== existing.payloadDigest) throw new CaptureConflictError();
      return copyCapture(existing);
    }
    if ([...this.ends.values()].some((end) => end.sessionId === input.sessionId)) throw new CaptureConflictError("session_end_immutable");
    const record = { ...input, key, deviceId, deviceOrder: ++this.deviceOrder, eventId, operationId, recordId: input.recordId ?? null, payloadDigest: "", attemptedWorkflowVersion: null, attemptedPayloadDigest: null, state: "Saved on device" as const, lastError: null };
    record.payloadDigest = await captureDigest(record);
    this.captures.set(key, record);
    return copyCapture(record);
  }

  public async listCaptures(sessionId: PublicId): Promise<StoredCapture[]> {
    return [...this.captures.values()]
      .filter((capture) => capture.sessionId === sessionId)
      .sort((a, b) => a.deviceOrder - b.deviceOrder || `${a.deviceId}\u0000${a.operationId}`.localeCompare(`${b.deviceId}\u0000${b.operationId}`))
      .map(copyCapture);
  }

  public async updateCapture(key: string, state: CaptureState, lastError: string | null = null): Promise<StoredCapture> {
    const current = this.captures.get(key);
    if (!current) throw new CaptureStorageError("capture_not_found");
    const updated = { ...current, state, lastError };
    this.captures.set(key, updated);
    return copyCapture(updated);
  }

  public async updateCaptureBinding(key: string, workflowVersion: number, payloadDigest: Digest): Promise<StoredCapture> {
    const current = this.captures.get(key);
    if (!current) throw new CaptureStorageError("capture_not_found");
    const updated = { ...current, attemptedWorkflowVersion: workflowVersion, attemptedPayloadDigest: payloadDigest };
    this.captures.set(key, updated);
    return copyCapture(updated);
  }

  public async saveEnd(input: EndInput): Promise<StoredEndIntent> {
    assertContext(input);
    const deviceId = await this.getDeviceId();
    const existing = await this.getEnd(input.sessionId);
    const operationId = input.operationId ?? existing?.operationId ?? id("operation_end");
    const requiredOperationIds = sortedReceipts(input.requiredOperationIds ?? (await this.listCaptures(input.sessionId)).map(({ deviceId: captureDeviceId, operationId: captureOperationId }) => ({ deviceId: captureDeviceId, operationId: captureOperationId })));
    const candidate = { ...input, key: endKey(input.sessionId, deviceId, operationId), deviceId, operationId, requiredOperationIds, payloadDigest: "", attemptedWorkflowVersion: null, attemptedPayloadDigest: null, state: "Saved on device" as const, lastError: null };
    candidate.payloadDigest = await endDigest(candidate);
    if (existing) {
      if (candidate.payloadDigest !== existing.payloadDigest) throw new CaptureConflictError("session end is already bound to a different operation set");
      return copyEnd(existing);
    }
    this.ends.set(candidate.key, candidate);
    return copyEnd(candidate);
  }

  public async getEnd(sessionId: PublicId): Promise<StoredEndIntent | null> {
    const value = [...this.ends.values()].find((end) => end.sessionId === sessionId);
    return value ? copyEnd(value) : null;
  }

  public async updateEnd(key: string, state: CaptureState, lastError: string | null = null): Promise<StoredEndIntent> {
    const current = [...this.ends.values()].find((end) => end.key === key);
    if (!current) throw new CaptureStorageError("end_not_found");
    const updated = { ...current, state, lastError };
    this.ends.set(key, updated);
    return copyEnd(updated);
  }

  public async updateEndBinding(key: string, workflowVersion: number, payloadDigest: Digest): Promise<StoredEndIntent> {
    const current = [...this.ends.values()].find((end) => end.key === key);
    if (!current) throw new CaptureStorageError("end_not_found");
    const updated = { ...current, attemptedWorkflowVersion: workflowVersion, attemptedPayloadDigest: payloadDigest };
    this.ends.set(key, updated);
    return copyEnd(updated);
  }
}

const databaseName = "warden-drydock-live-capture";
const databaseVersion = 1;
const metaStore = "meta";
const captureStore = "captures";
const endStore = "ends";
const deviceMetaKey = "device_id";
const orderMetaKey = "device_order";

interface MetaRow { key: string; value: string | number; }

class EndSnapshotChangedError extends Error {}
class CaptureOrderChangedError extends Error {}

function openDatabase(): Promise<IDBDatabase> {
  if (!globalThis.indexedDB) return Promise.reject(new CaptureStorageError());
  return new Promise((resolve, reject) => {
    const request = globalThis.indexedDB.open(databaseName, databaseVersion);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(metaStore)) database.createObjectStore(metaStore, { keyPath: "key" });
      if (!database.objectStoreNames.contains(captureStore)) {
        const store = database.createObjectStore(captureStore, { keyPath: "key" });
        store.createIndex("sessionId", "sessionId", { unique: false });
      }
      if (!database.objectStoreNames.contains(endStore)) {
        const store = database.createObjectStore(endStore, { keyPath: "key" });
        store.createIndex("sessionId", "sessionId", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new CaptureStorageError(request.error?.message ?? "indexeddb_open_failed"));
  });
}

class IndexedDbCaptureStore implements CaptureStore {
  private async readMeta(key: string): Promise<MetaRow | undefined> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(metaStore, "readonly");
      const request = transaction.objectStore(metaStore).get(key);
      request.onsuccess = () => resolve(request.result as MetaRow | undefined);
      request.onerror = () => reject(new CaptureStorageError(request.error?.message ?? "indexeddb_read_failed"));
      transaction.oncomplete = () => database.close();
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_read_failed")); };
    });
  }

  public async getDeviceId(): Promise<PublicId> {
    const current = await this.readMeta(deviceMetaKey);
    if (current) return current.value as PublicId;
    const generated = id("device");
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(metaStore, "readwrite");
      const store = transaction.objectStore(metaStore);
      const request = store.get(deviceMetaKey);
      let result = generated;
      request.onsuccess = () => {
        const existing = request.result as MetaRow | undefined;
        if (existing) result = existing.value as PublicId;
        else store.put({ key: deviceMetaKey, value: generated } satisfies MetaRow);
      };
      request.onerror = () => { database.close(); reject(new CaptureStorageError(request.error?.message ?? "indexeddb_device_read_failed")); };
      transaction.oncomplete = () => { database.close(); resolve(result); };
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_device_write_failed")); };
    });
  }

  private async getCapture(key: string): Promise<StoredCapture | undefined> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(captureStore, "readonly");
      const request = transaction.objectStore(captureStore).get(key);
      request.onsuccess = () => resolve(request.result as StoredCapture | undefined);
      request.onerror = () => reject(new CaptureStorageError(request.error?.message ?? "indexeddb_capture_read_failed"));
      transaction.oncomplete = () => database.close();
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_capture_read_failed")); };
    });
  }

  public async saveCapture(input: CaptureInput): Promise<StoredCapture> {
    assertCaptureInput(input);
    const deviceId = await this.getDeviceId();
    const operationId = input.operationId ?? id("operation");
    const eventId = input.eventId ?? id("event");
    const key = captureKey(input.sessionId, deviceId, operationId);
    const existing = await this.getCapture(key);
    if (existing) {
      const candidate = await captureDigest({ ...input, deviceId, operationId, eventId: input.eventId ?? existing.eventId, deviceOrder: existing.deviceOrder, recordId: input.recordId ?? null });
      if (candidate !== existing.payloadDigest) throw new CaptureConflictError();
      return copyCapture(existing);
    }
    while (true) {
      const currentOrder = await this.readMeta(orderMetaKey);
      const priorOrder = typeof currentOrder?.value === "number" ? currentOrder.value : 0;
      const deviceOrder = priorOrder + 1;
      const record: StoredCapture = { ...input, key, deviceId, deviceOrder, eventId, operationId, recordId: input.recordId ?? null, payloadDigest: "", attemptedWorkflowVersion: null, attemptedPayloadDigest: null, state: "Saved on device", lastError: null };
      record.payloadDigest = await captureDigest(record);
      const database = await openDatabase();
      try {
        await new Promise<void>((resolve, reject) => {
          const transaction = database.transaction([metaStore, captureStore, endStore], "readwrite");
          const meta = transaction.objectStore(metaStore);
          const captures = transaction.objectStore(captureStore);
          const ends = transaction.objectStore(endStore);
          const existingRequest = captures.get(key);
          const endsRequest = ends.index("sessionId").getAll(input.sessionId);
          const orderRequest = meta.get(orderMetaKey);
          let existingReady = false;
          let endReady = false;
          let orderReady = false;
          let existingInTransaction: StoredCapture | undefined;
          let endsInTransaction: StoredEndIntent[] = [];
          let transactionOrder = 0;
          let failure: Error | undefined;
          const abort = (error: Error) => {
            failure = error;
            transaction.abort();
          };
          const maybeAdd = () => {
            if (!existingReady || !endReady || !orderReady) return;
            if (existingInTransaction) {
              abort(new CaptureConflictError());
              return;
            }
            if (endsInTransaction.length > 0) {
              abort(new CaptureConflictError("session_end_immutable"));
              return;
            }
            if (transactionOrder !== priorOrder) {
              abort(new CaptureOrderChangedError());
              return;
            }
            meta.put({ key: orderMetaKey, value: deviceOrder } satisfies MetaRow);
            const addRequest = captures.add(record);
            addRequest.onerror = () => abort(addRequest.error?.name === "ConstraintError" ? new CaptureConflictError() : new CaptureStorageError(addRequest.error?.message ?? "indexeddb_capture_write_failed"));
          };
          existingRequest.onsuccess = () => {
            existingInTransaction = existingRequest.result as StoredCapture | undefined;
            existingReady = true;
            maybeAdd();
          };
          endsRequest.onsuccess = () => {
            endsInTransaction = endsRequest.result as StoredEndIntent[];
            endReady = true;
            maybeAdd();
          };
          orderRequest.onsuccess = () => {
            const stored = orderRequest.result as MetaRow | undefined;
            transactionOrder = typeof stored?.value === "number" ? stored.value : 0;
            orderReady = true;
            maybeAdd();
          };
          existingRequest.onerror = () => abort(new CaptureStorageError(existingRequest.error?.message ?? "indexeddb_capture_read_failed"));
          endsRequest.onerror = () => abort(new CaptureStorageError(endsRequest.error?.message ?? "indexeddb_end_read_failed"));
          orderRequest.onerror = () => abort(new CaptureStorageError(orderRequest.error?.message ?? "indexeddb_order_read_failed"));
          transaction.oncomplete = () => { database.close(); resolve(); };
          transaction.onabort = () => { database.close(); reject(failure ?? new CaptureStorageError("indexeddb_capture_write_failed")); };
          transaction.onerror = () => { failure ??= new CaptureStorageError(transaction.error?.message ?? "indexeddb_capture_write_failed"); };
        });
        return copyCapture(record);
      } catch (error) {
        if (error instanceof CaptureOrderChangedError) continue;
        if (error instanceof CaptureConflictError) {
          const concurrent = await this.getCapture(key);
          if (concurrent) {
            const replayDigest = await captureDigest({ ...input, deviceId, operationId, eventId: input.eventId ?? concurrent.eventId, deviceOrder: concurrent.deviceOrder, recordId: input.recordId ?? null });
            if (concurrent.payloadDigest === replayDigest) return copyCapture(concurrent);
          }
        }
        throw error;
      }
    }
  }

  public async listCaptures(sessionId: PublicId): Promise<StoredCapture[]> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(captureStore, "readonly");
      const request = transaction.objectStore(captureStore).index("sessionId").getAll(sessionId);
      request.onsuccess = () => resolve((request.result as StoredCapture[]).sort((a, b) => a.deviceOrder - b.deviceOrder || `${a.deviceId}\u0000${a.operationId}`.localeCompare(`${b.deviceId}\u0000${b.operationId}`)).map(copyCapture));
      request.onerror = () => reject(new CaptureStorageError(request.error?.message ?? "indexeddb_capture_list_failed"));
      transaction.oncomplete = () => database.close();
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_capture_list_failed")); };
    });
  }

  public async updateCapture(key: string, state: CaptureState, lastError: string | null = null): Promise<StoredCapture> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(captureStore, "readwrite");
      const store = transaction.objectStore(captureStore);
      const request = store.get(key);
      let updated: StoredCapture | undefined;
      request.onsuccess = () => {
        const current = request.result as StoredCapture | undefined;
        if (!current) { database.close(); reject(new CaptureStorageError("capture_not_found")); return; }
        updated = { ...current, state, lastError };
        store.put(updated);
      };
      request.onerror = () => { database.close(); reject(new CaptureStorageError(request.error?.message ?? "indexeddb_capture_read_failed")); };
      transaction.oncomplete = () => { database.close(); resolve(copyCapture(updated!)); };
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_capture_write_failed")); };
    });
  }

  public async updateCaptureBinding(key: string, workflowVersion: number, payloadDigest: Digest): Promise<StoredCapture> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(captureStore, "readwrite");
      const store = transaction.objectStore(captureStore);
      const request = store.get(key);
      let updated: StoredCapture | undefined;
      request.onsuccess = () => {
        const current = request.result as StoredCapture | undefined;
        if (!current) { database.close(); reject(new CaptureStorageError("capture_not_found")); return; }
        updated = { ...current, attemptedWorkflowVersion: workflowVersion, attemptedPayloadDigest: payloadDigest };
        store.put(updated);
      };
      request.onerror = () => { database.close(); reject(new CaptureStorageError(request.error?.message ?? "indexeddb_capture_read_failed")); };
      transaction.oncomplete = () => { database.close(); resolve(copyCapture(updated!)); };
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_capture_write_failed")); };
    });
  }

  private async getEndBySession(sessionId: PublicId): Promise<StoredEndIntent | undefined> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(endStore, "readonly");
      const request = transaction.objectStore(endStore).index("sessionId").getAll(sessionId);
      request.onsuccess = () => resolve((request.result as StoredEndIntent[])[0]);
      request.onerror = () => reject(new CaptureStorageError(request.error?.message ?? "indexeddb_end_read_failed"));
      transaction.oncomplete = () => database.close();
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_end_read_failed")); };
    });
  }

  public async saveEnd(input: EndInput): Promise<StoredEndIntent> {
    assertContext(input);
    const deviceId = await this.getDeviceId();
    const operationId = input.operationId ?? id("operation_end");
    const useCaptureWatermark = input.requiredOperationIds === undefined;
    while (true) {
      const requiredOperationIds = sortedReceipts(input.requiredOperationIds ?? (await this.listCaptures(input.sessionId)).map(({ deviceId: captureDeviceId, operationId: captureOperationId }) => ({ deviceId: captureDeviceId, operationId: captureOperationId })));
      const candidate: StoredEndIntent = { ...input, key: endKey(input.sessionId, deviceId, operationId), deviceId, operationId, requiredOperationIds, payloadDigest: "", attemptedWorkflowVersion: null, attemptedPayloadDigest: null, state: "Saved on device", lastError: null };
      candidate.payloadDigest = await endDigest(candidate);
      const database = await openDatabase();
      let existing: StoredEndIntent | undefined;
      try {
        existing = await new Promise<StoredEndIntent | undefined>((resolve, reject) => {
          const transaction = database.transaction([captureStore, endStore], "readwrite");
          const captures = transaction.objectStore(captureStore);
          const ends = transaction.objectStore(endStore);
          const existingRequest = ends.index("sessionId").getAll(input.sessionId);
          const capturesRequest = captures.index("sessionId").getAll(input.sessionId);
          let existingReady = false;
          let capturesReady = false;
          let capturedRows: StoredCapture[] = [];
          let failure: Error | undefined;
          const abort = (error: Error) => {
            failure = error;
            transaction.abort();
          };
          const maybeWrite = () => {
            if (!existingReady || !capturesReady) return;
            existing = (existingRequest.result as StoredEndIntent[])[0];
            if (existing) return;
            if (useCaptureWatermark) {
              const currentRequiredOperationIds = sortedReceipts(capturedRows.map(({ deviceId: captureDeviceId, operationId: captureOperationId }) => ({ deviceId: captureDeviceId, operationId: captureOperationId })));
              const snapshotMatches = currentRequiredOperationIds.length === requiredOperationIds.length
                && currentRequiredOperationIds.every((receipt, index) => receipt.deviceId === requiredOperationIds[index].deviceId && receipt.operationId === requiredOperationIds[index].operationId);
              if (!snapshotMatches) {
                abort(new EndSnapshotChangedError());
                return;
              }
            }
            const addRequest = ends.add(candidate);
            addRequest.onerror = () => abort(addRequest.error?.name === "ConstraintError" ? new CaptureConflictError() : new CaptureStorageError(addRequest.error?.message ?? "indexeddb_end_write_failed"));
          };
          existingRequest.onsuccess = () => {
            existingReady = true;
            maybeWrite();
          };
          capturesRequest.onsuccess = () => {
            capturedRows = capturesRequest.result as StoredCapture[];
            capturesReady = true;
            maybeWrite();
          };
          existingRequest.onerror = () => abort(new CaptureStorageError(existingRequest.error?.message ?? "indexeddb_end_read_failed"));
          capturesRequest.onerror = () => abort(new CaptureStorageError(capturesRequest.error?.message ?? "indexeddb_capture_read_failed"));
          transaction.oncomplete = () => { database.close(); resolve(existing); };
          transaction.onabort = () => { database.close(); reject(failure ?? new CaptureStorageError("indexeddb_end_write_failed")); };
          transaction.onerror = () => { failure ??= new CaptureStorageError(transaction.error?.message ?? "indexeddb_end_write_failed"); };
        });
      } catch (error) {
        if (error instanceof EndSnapshotChangedError) continue;
        throw error;
      }
      if (!existing) return copyEnd(candidate);
      const replayCandidate = { ...input, key: existing.key, deviceId: existing.deviceId, operationId: input.operationId ?? existing.operationId, requiredOperationIds, payloadDigest: "", state: existing.state, lastError: existing.lastError };
      replayCandidate.payloadDigest = await endDigest(replayCandidate);
      if (replayCandidate.payloadDigest !== existing.payloadDigest) throw new CaptureConflictError("session end is already bound to a different operation set");
      return copyEnd(existing);
    }
  }

  public async getEnd(sessionId: PublicId): Promise<StoredEndIntent | null> {
    const value = await this.getEndBySession(sessionId);
    return value ? copyEnd(value) : null;
  }

  public async updateEnd(key: string, state: CaptureState, lastError: string | null = null): Promise<StoredEndIntent> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(endStore, "readwrite");
      const store = transaction.objectStore(endStore);
      const request = store.get(key);
      let updated: StoredEndIntent | undefined;
      request.onsuccess = () => {
        const current = request.result as StoredEndIntent | undefined;
        if (!current) { database.close(); reject(new CaptureStorageError("end_not_found")); return; }
        updated = { ...current, state, lastError };
        store.put(updated);
      };
      request.onerror = () => { database.close(); reject(new CaptureStorageError(request.error?.message ?? "indexeddb_end_read_failed")); };
      transaction.oncomplete = () => { database.close(); resolve(copyEnd(updated!)); };
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_end_write_failed")); };
    });
  }

  public async updateEndBinding(key: string, workflowVersion: number, payloadDigest: Digest): Promise<StoredEndIntent> {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(endStore, "readwrite");
      const store = transaction.objectStore(endStore);
      const request = store.get(key);
      let updated: StoredEndIntent | undefined;
      request.onsuccess = () => {
        const current = request.result as StoredEndIntent | undefined;
        if (!current) { database.close(); reject(new CaptureStorageError("end_not_found")); return; }
        updated = { ...current, attemptedWorkflowVersion: workflowVersion, attemptedPayloadDigest: payloadDigest };
        store.put(updated);
      };
      request.onerror = () => { database.close(); reject(new CaptureStorageError(request.error?.message ?? "indexeddb_end_read_failed")); };
      transaction.oncomplete = () => { database.close(); resolve(copyEnd(updated!)); };
      transaction.onerror = () => { database.close(); reject(new CaptureStorageError(transaction.error?.message ?? "indexeddb_end_write_failed")); };
    });
  }
}

export function createIndexedDbCaptureStore(): CaptureStore {
  return new IndexedDbCaptureStore();
}

export class CaptureQueue {
  public constructor(private readonly store: CaptureStore, private readonly transport: CaptureSyncTransport) {}

  public capture(input: CaptureInput): Promise<StoredCapture> {
    return this.store.saveCapture(input);
  }

  public end(input: EndInput): Promise<StoredEndIntent> {
    return this.store.saveEnd(input);
  }

  public async sync(sessionId: PublicId): Promise<CaptureSyncResult> {
    let captures = await this.store.listCaptures(sessionId);
    let end = await this.store.getEnd(sessionId);
    let workflowVersion: number | undefined;
    let observedSession: SessionSyncResponse | undefined;
    const summarize = async (): Promise<CaptureSyncResult> => {
      const finalCaptures = await this.store.listCaptures(sessionId);
      const finalEnd = await this.store.getEnd(sessionId);
      return {
        captures: finalCaptures.map(({ key, state }) => ({ key, state })),
        end: finalEnd ? { key: finalEnd.key, state: finalEnd.state } : null,
      };
    };
    const reconcileObservedReceipts = async (): Promise<void> => {
      if (!observedSession?.acknowledgements) return;
      const acknowledged = new Map(observedSession.acknowledgements.map((receipt) => [`${receipt.deviceId}\u0000${receipt.operationId}`, receipt]));
      for (const capture of captures) {
        if (capture.state === "Synced" || capture.state === "Needs attention") continue;
        const receipt = acknowledged.get(`${capture.deviceId}\u0000${capture.operationId}`);
        if (!receipt) continue;
        const matches = receipt.outcome !== "digest_conflict" && receipt.payloadDigest === (capture.attemptedPayloadDigest ?? capture.payloadDigest);
        await this.store.updateCapture(capture.key, matches ? "Synced" : "Needs attention", matches ? null : "idempotency_digest_conflict");
      }
      if (end && end.state !== "Synced" && end.state !== "Needs attention") {
        const receipt = acknowledged.get(`${end.deviceId}\u0000${end.operationId}`);
        if (receipt) {
          const matches = receipt.outcome !== "digest_conflict" && receipt.payloadDigest === (end.attemptedPayloadDigest ?? end.payloadDigest);
          await this.store.updateEnd(end.key, matches ? "Synced" : "Needs attention", matches ? null : "idempotency_digest_conflict");
        }
      }
    };
    const surfaceEndedSession = async (): Promise<CaptureSyncResult> => {
      const message = "live_session_ended";
      for (const capture of captures) {
        if (capture.state !== "Synced") await this.store.updateCapture(capture.key, "Needs attention", message);
      }
      if (end && end.state !== "Synced") await this.store.updateEnd(end.key, "Needs attention", message);
      return summarize();
    };

    if (this.transport.readSession && (captures.length > 0 || end !== null)) {
      try {
        observedSession = await this.transport.readSession(captures[0]?.campaignId ?? end!.campaignId, sessionId);
        workflowVersion = observedSession.workflowVersion;
        await reconcileObservedReceipts();
        captures = await this.store.listCaptures(sessionId);
        end = await this.store.getEnd(sessionId);
        if (observedSession.mode && observedSession.mode !== "active") return surfaceEndedSession();
      } catch (observationError) {
        if (!isRetryableCaptureError(observationError)) {
          const message = observationError instanceof Error ? observationError.message : "session_observe_failed";
          for (const capture of captures) {
            if (capture.state !== "Synced") await this.store.updateCapture(capture.key, "Needs attention", message);
          }
          if (end && end.state !== "Synced") await this.store.updateEnd(end.key, "Needs attention", message);
        }
        return summarize();
      }
    }

    for (const capture of captures) {
      if (capture.state === "Synced" || capture.state === "Needs attention") continue;
      await this.store.updateCapture(capture.key, "Syncing", null);
      try {
        const sendWorkflowVersion = workflowVersion ?? capture.workflowVersion;
        const attemptedPayloadDigest = await captureDigest({ ...capture, workflowVersion: sendWorkflowVersion });
        const boundCapture = await this.store.updateCaptureBinding(capture.key, sendWorkflowVersion, attemptedPayloadDigest);
        const response = await this.transport.sendCapture(boundCapture, sendWorkflowVersion);
        workflowVersion = response.workflowVersion;
        const outcome = response.outcome;
        const state = outcome === "accepted" || outcome === "exact_replay" ? "Synced" : outcome === "digest_conflict" ? "Needs attention" : "Needs attention";
        await this.store.updateCapture(capture.key, state, state === "Needs attention" ? "capture_outcome_invalid" : null);
      } catch (error) {
        const retryable = isRetryableCaptureError(error);
        await this.store.updateCapture(capture.key, retryable ? "Saved on device" : "Needs attention", error instanceof Error ? error.message : "capture_sync_failed");
        if (retryable) break;
      }
    }
    captures = await this.store.listCaptures(sessionId);
    end = await this.store.getEnd(sessionId);
    if (end && end.state !== "Synced" && end.state !== "Needs attention") {
      const localDeviceId = await this.store.getDeviceId();
      let observationError: unknown = null;
      if (this.transport.readSession) {
        try {
          observedSession = await this.transport.readSession(end.campaignId, sessionId);
          workflowVersion = observedSession.workflowVersion;
          await reconcileObservedReceipts();
          captures = await this.store.listCaptures(sessionId);
          end = await this.store.getEnd(sessionId);
        } catch (error) {
          observationError = error;
        }
      }
      if (observationError) {
        const retryable = isRetryableCaptureError(observationError);
        if (end) await this.store.updateEnd(end.key, retryable ? "Saved on device" : "Needs attention", observationError instanceof Error ? observationError.message : "session_observe_failed");
      } else if (observedSession?.mode && observedSession.mode !== "active") {
        return surfaceEndedSession();
      } else if (!end || end.state === "Synced" || end.state === "Needs attention") {
        return summarize();
      } else {
        const byIdentity = new Map(captures.map((capture) => [`${capture.deviceId}\u0000${capture.operationId}`, capture]));
        const acknowledged = new Set((observedSession?.acknowledgedOperationIds ?? []).map(({ deviceId, operationId }) => `${deviceId}\u0000${operationId}`));
        const serverAcknowledgementsAvailable = Boolean(this.transport.readSession);
        const missingLocal = end.requiredOperationIds.some((receipt) => {
          const key = `${receipt.deviceId}\u0000${receipt.operationId}`;
          return receipt.deviceId === localDeviceId && !byIdentity.has(key) && (!serverAcknowledgementsAvailable || !acknowledged.has(key));
        });
        const pending = end.requiredOperationIds.some((receipt) => {
          const key = `${receipt.deviceId}\u0000${receipt.operationId}`;
          const capture = byIdentity.get(key);
          if (capture && capture.state !== "Synced") return true;
          if (serverAcknowledgementsAvailable) return !acknowledged.has(key);
          return capture === undefined ? receipt.deviceId !== localDeviceId : capture.state !== "Synced";
        });
        const requiredKeys = new Set(end.requiredOperationIds.map(({ deviceId, operationId }) => `${deviceId}\u0000${operationId}`));
        const serverCaptureKeys = new Set((observedSession?.captureOperationIds ?? observedSession?.acknowledgedOperationIds ?? []).map(({ deviceId, operationId }) => `${deviceId}\u0000${operationId}`));
        const hiddenServerCapture = serverAcknowledgementsAvailable && [...serverCaptureKeys].some((key) => !requiredKeys.has(key));
        if (missingLocal) await this.store.updateEnd(end.key, "Needs attention", "required_capture_missing");
        else if (hiddenServerCapture) await this.store.updateEnd(end.key, "Needs attention", "live_barrier_conflict");
        else if (pending) await this.store.updateEnd(end.key, "Saved on device", serverAcknowledgementsAvailable ? "captures_pending" : "remote_acknowledgements_unavailable");
        else {
          await this.store.updateEnd(end.key, "Syncing", null);
          try {
            const sendWorkflowVersion = workflowVersion ?? end.workflowVersion;
            const attemptedPayloadDigest = await endDigest({ ...end, workflowVersion: sendWorkflowVersion });
            const boundEnd = await this.store.updateEndBinding(end.key, sendWorkflowVersion, attemptedPayloadDigest);
            const result = await this.transport.sendEnd(boundEnd, sendWorkflowVersion);
            await this.store.updateEnd(end.key, result.readyForProposal ? "Synced" : "Saved on device", result.readyForProposal ? null : "live_barrier_pending");
          } catch (error) {
            await this.store.updateEnd(end.key, isRetryableCaptureError(error) ? "Saved on device" : "Needs attention", error instanceof Error ? error.message : "end_sync_failed");
          }
        }
      }
    }
    return summarize();
  }
}
