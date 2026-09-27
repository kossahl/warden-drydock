import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { AtlasApi } from "../api/atlasClient";
import { browserId, type SliceApi } from "../api/client";
import { httpCaptureTransport, httpLiveClient, type LiveClient } from "../api/liveClient";
import type { AtlasCampaignItem, GenerationAction, GenerationEvent, GenerationView, LiveEvent, LiveSessionView, ProviderReadiness, SaveSyncState } from "../contracts/v2";
import { CaptureQueue, createIndexedDbCaptureStore, type StoredCapture } from "./captureStore";
import type { Navigate } from "../atlas/AtlasCompletion";

const store = createIndexedDbCaptureStore();
const queue = new CaptureQueue(store, httpCaptureTransport);
const controllerIdentity = globalThis.crypto?.randomUUID
  ? `controller_${globalThis.crypto.randomUUID().replaceAll("-", "")}`
  : browserId("controller");
const sessionCacheKey = (sessionId: string) => `warden-live-session:${sessionId}`;
const cacheSession = (session: LiveSessionView) => {
  try { sessionStorage.setItem(sessionCacheKey(session.session_id), JSON.stringify(session)); } catch { /* A server read remains available when browser storage is disabled. */ }
};
const sessionHref = (campaignId: string, revision: string, sessionId?: string) => {
  const params = new URLSearchParams({ revision });
  if (sessionId) params.set("session", sessionId);
  return `/campaigns/${encodeURIComponent(campaignId)}/live?${params}`;
};
const errorText = (failure: unknown) => failure instanceof Error ? failure.message : "request_failed";

export function LiveCockpit({ campaign, initialHead, api, atlasApi, readiness, navigate, sessionId, revisionId, liveClient = httpLiveClient }: {
  campaign: AtlasCampaignItem;
  initialHead: string;
  api: SliceApi;
  atlasApi: AtlasApi;
  readiness: ProviderReadiness | null;
  navigate: Navigate;
  sessionId: string | null;
  revisionId: string;
  liveClient?: LiveClient;
}) {
  const [session, setSession] = useState<LiveSessionView | null>(null);
  const [sessionReachable, setSessionReachable] = useState(true);
  const [head, setHead] = useState(initialHead);
  const [items, setItems] = useState<StoredCapture[]>([]);
  const [type, setType] = useState<"confirmed_fact" | "unresolved_question">("confirmed_fact");
  const [captureText, setCaptureText] = useState("");
  const [recordId, setRecordId] = useState("");
  const [action, setAction] = useState<GenerationAction>("ask");
  const [prompt, setPrompt] = useState("");
  const [generation, setGeneration] = useState<GenerationView | null>(null);
  const [draft, setDraft] = useState("");
  const [sequence, setSequence] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [syncStates, setSyncStates] = useState<Record<string, SaveSyncState>>({});
  const [endStatus, setEndStatus] = useState<SaveSyncState | null>(null);
  const [draftContext, setDraftContext] = useState<StoredCapture | null>(null);
  const ownController = controllerIdentity;
  const controller = session?.controller.controller_id === ownController;
  const aiReady = readiness?.ai_available === true;
  const activeSessionId = session?.session_id ?? sessionId;

  const refreshLocal = useCallback(async () => {
    if (!activeSessionId) return;
    const captures = await store.listCaptures(activeSessionId);
    setItems(captures);
    if (!controller && captures.some((capture) => capture.lastError?.includes("stale_controller"))) setError("This tab lost live control. Its capture remains saved locally. Refresh the session and take over before writing again.");
    const end = await store.getEnd(activeSessionId);
    setEndStatus(end?.state ?? null);
  }, [activeSessionId, controller]);

  const observe = useCallback(async () => {
    try {
      const current = await liveClient.observe(campaign.campaign_id);
      if (sessionId && current.session_id !== sessionId) throw new Error("session_observe_mismatch");
      setSession(current);
      cacheSession(current);
      setSessionReachable(true);
      setHead(current.reported_head_revision);
      if (!sessionId) navigate(sessionHref(campaign.campaign_id, current.base_revision, current.session_id), true);
    } catch (failure) {
      if (sessionId) {
        try {
          const cached = JSON.parse(sessionStorage.getItem(sessionCacheKey(sessionId)) ?? "null") as LiveSessionView | null;
          if (cached?.session_id === sessionId && cached.campaign_id === campaign.campaign_id) { setSession(cached); setSessionReachable(false); }
        } catch { /* Invalid cached state is ignored; the server remains authoritative. */ }
      }
      if (sessionId || !(failure && typeof failure === "object" && "status" in failure && failure.status === 404)) setError(`Session status could not be refreshed (${errorText(failure)}).`);
    }
  }, [campaign.campaign_id, liveClient, navigate, sessionId]);

  useEffect(() => { void observe(); const timer = window.setInterval(() => void observe(), 5000); return () => window.clearInterval(timer); }, [observe]);
  useEffect(() => { void refreshLocal(); }, [refreshLocal]);
  useEffect(() => {
    let cancelled = false;
    void atlasApi.campaigns().then((result) => {
      const updated = result.campaigns.find((item) => item.campaign_id === campaign.campaign_id);
      if (!cancelled && updated) setHead(updated.head_revision.revision_id);
    }).catch(() => undefined);
    const timer = window.setInterval(() => {
      void atlasApi.campaigns().then((result) => {
        const updated = result.campaigns.find((item) => item.campaign_id === campaign.campaign_id);
        if (!cancelled && updated) setHead(updated.head_revision.revision_id);
      }).catch(() => undefined);
    }, 10000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [atlasApi, campaign.campaign_id]);
  useEffect(() => {
    if (!activeSessionId || !controller || session?.mode !== "active") return;
    const onOnline = () => void sync();
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
    // The callback is intentionally recreated with the latest session binding.
  });

  async function start() {
    setBusy(true); setError("");
    try {
      const currentCampaign = (await atlasApi.campaigns()).campaigns.find((item) => item.campaign_id === campaign.campaign_id);
      if (!currentCampaign) throw new Error("campaign_not_found");
      const currentHead = currentCampaign.head_revision.revision_id;
      setHead(currentHead);
      const started = await liveClient.start(campaign.campaign_id, currentHead, ownController);
      setSession(started);
      cacheSession(started);
      setSessionReachable(true);
      navigate(sessionHref(campaign.campaign_id, started.base_revision, started.session_id));
      setAnnouncement(`Live session started at ${started.base_revision}.`);
    } catch (failure) { setError(`Could not start the live session (${errorText(failure)}).`); }
    finally { setBusy(false); }
  }

  async function takeover() {
    if (!session) return;
    setBusy(true); setError("");
    try { const next = await liveClient.takeover(session, ownController); setSession(next); cacheSession(next); setSessionReachable(true); setAnnouncement("Control taken over in this tab. Other tabs are read only."); }
    catch (failure) { setError(`Takeover was rejected by the live session (${errorText(failure)}). Refresh session status before writing.`); await observe(); }
    finally { setBusy(false); }
  }

  async function sync() {
    if (!activeSessionId || !controller) return;
    try {
      const result = await queue.sync(activeSessionId, ownController);
      const states: Record<string, SaveSyncState> = {};
      result.captures.forEach((item) => { states[item.key] = item.state; });
      if (result.end) states[result.end.key] = result.end.state;
      setSyncStates(states); setEndStatus(result.end?.state ?? null); await refreshLocal();
      if (result.end?.state === "Synced") { setAnnouncement("Ended. Server acknowledged the complete required operation set; review is ready."); await observe(); }
    } catch (failure) { setError(`Sync could not finish (${errorText(failure)}). Local capture remains saved on this device.`); }
  }

  async function capture(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!session || !controller || !captureText.trim()) return;
    setBusy(true); setError("");
    try {
      const saved = await queue.capture({ campaignId: session.campaign_id, sessionId: session.session_id, baseRevision: session.base_revision, controllerId: session.controller.controller_id, controllerEpoch: session.controller.epoch, workflowVersion: session.workflow_version, captureType: type, text: captureText.trim(), recordId: recordId.trim() || null });
      setItems(await store.listCaptures(session.session_id)); setCaptureText(""); setAnnouncement(`${type === "confirmed_fact" ? "Confirmed table fact" : "Unresolved question"} saved on device.`);
      void sync();
      if (saved.state === "Needs attention") setError("This capture needs attention and remains stored on this device.");
    } catch (failure) { setError(`Capture was not saved (${errorText(failure)}).`); }
    finally { setBusy(false); }
  }

  async function runAi(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!session || !controller || !aiReady || !prompt.trim()) return;
    const provenanceCapture = action === "generate" ? draftContext : null;
    setBusy(true); setError(""); setDraft(""); setSequence(0);
    try {
      const started = await api.startGeneration(session.campaign_id, session.base_revision, action, prompt.trim(), browserId("generation"), { scope: "campaign" }, session.session_id);
      setGeneration(started); setDraftContext(provenanceCapture); setAnnouncement(`${action[0].toUpperCase()}${action.slice(1)} Draft started at ${started.source_revision}.`);
      const events: GenerationEvent[] = await api.resumeGeneration(started.generation_id, 0);
      setDraft((value) => value + events.map((entry) => entry.draft_fragment ?? "").join(""));
      setSequence(events.reduce((max, entry) => Math.max(max, entry.sequence), 0));
      setGeneration(await api.readGeneration(started.generation_id));
    } catch (failure) { setError(`The ${action} request did not complete (${errorText(failure)}). Captures remain available.`); }
    finally { setBusy(false); }
  }

  async function resumeDraft() {
    if (!generation) return;
    setBusy(true); setError("");
    try {
      const events = await api.resumeGeneration(generation.generation_id, sequence);
      const fresh = events.filter((entry) => entry.sequence > sequence);
      setDraft((value) => value + fresh.map((entry) => entry.draft_fragment ?? "").join(""));
      setSequence(events.reduce((max, entry) => Math.max(max, entry.sequence), sequence));
      setGeneration(await api.readGeneration(generation.generation_id));
    } catch (failure) { setError(`Draft stream could not resume (${errorText(failure)}).`); }
    finally { setBusy(false); }
  }

  async function end() {
    if (!session || !controller) return;
    if (!window.confirm("End this live session? Unsynced captures will remain on this device until the server acknowledges them.")) return;
    setBusy(true); setError("");
    try {
      const local = await store.listCaptures(session.session_id);
      const required = new Map(local.map(({ deviceId, operationId }) => [`${deviceId}\u0000${operationId}`, { deviceId, operationId }]));
      for (const receipt of session.acknowledgements) required.set(`${receipt.device_id}\u0000${receipt.operation_id}`, { deviceId: receipt.device_id, operationId: receipt.operation_id });
      await queue.end({ campaignId: session.campaign_id, sessionId: session.session_id, baseRevision: session.base_revision, controllerId: session.controller.controller_id, controllerEpoch: session.controller.epoch, workflowVersion: session.workflow_version, requiredOperationIds: [...required.values()] });
      setAnnouncement("Ended - review pending. Waiting for server acknowledgement of the exact operation set.");
      await sync();
    } catch (failure) { setError(`End intent was not saved (${errorText(failure)}).`); }
    finally { setBusy(false); }
  }

  const openRecord = (capture: Pick<StoredCapture, "campaignId" | "sessionId" | "baseRevision" | "operationId" | "recordId">) => {
    if (!capture.recordId) return;
    const params = new URLSearchParams({ revision: campaign.head_revision.revision_id, live_session: capture.sessionId, source_revision: capture.baseRevision, capture_operation: capture.operationId, affected_record: capture.recordId });
    navigate(`/campaigns/${encodeURIComponent(capture.campaignId)}/records/${encodeURIComponent(capture.recordId)}?${params}`);
  };
  const facts = items.filter((item) => item.captureType === "confirmed_fact");
  const questions = items.filter((item) => item.captureType === "unresolved_question");
  const acknowledgedOperations = new Set((session?.end_barrier?.acknowledged_operation_ids ?? []).map(({ device_id, operation_id }) => `${device_id}\u0000${operation_id}`));
  const reviewedCaptures = session?.end_barrier?.ready_for_proposal
    ? session.events.filter((item) => acknowledgedOperations.has(`${item.device_id}\u0000${item.operation_id}`) && item.record_id)
    : [];
  const canWrite = controller && session?.mode === "active" && endStatus === null;
  const editLink = (capture: StoredCapture) => capture.recordId ? <><button type="button" className="button-link" onClick={() => openRecord(capture)}>Open affected record in editor</button>{capture.captureType === "confirmed_fact" && <button type="button" onClick={() => { setDraftContext(capture); setRecordId(capture.recordId!); setAction("generate"); document.querySelector<HTMLElement>("#live-prompt")?.focus(); }}>Generate Draft from this fact</button>}</> : null;

  return <section className="live-cockpit" aria-labelledby="live-heading">
    <header className="live-header"><div><p className="eyebrow">Warden only · Live cockpit</p><h1 id="live-heading">{campaign.campaign_name}</h1></div><p className="live-revision"><strong>Live base revision</strong><br /><code>{session?.base_revision ?? revisionId}</code></p></header>
    {session && <section className="live-status" aria-label="Session authority and synchronization">
      <p><strong>Session</strong> <code>{session.session_id}</code></p><p><strong>Controller</strong> {controller ? "This tab controls the session" : "Observer · read only"} · epoch {session.controller.epoch}</p><p><strong>Mode</strong> {session.mode === "ended_review_pending" ? "Ended - review pending" : session.mode === "ended" ? "Ended" : "Active"}</p>
      {!sessionReachable && <p className="warning" role="status">Offline. Showing the last server-observed session state. Captures can be saved on this device and will be checked by the server when reconnected.</p>}
      {head !== session.base_revision && <p className="warning" role="status">Campaign head is now <code>{head}</code>. Live grounding remains pinned to <code>{session.base_revision}</code>.</p>}
      {session.mode !== "active" && <p role="status">This session has ended. No new capture or AI action is available.</p>}
      {!controller && session.mode === "active" && <button type="button" disabled={busy} onClick={() => void takeover()}>Take over control</button>}
    </section>}
    {session?.end_barrier?.ready_for_proposal && session.mode !== "active" && <section className="card" aria-labelledby="live-review-heading">
      <h2 id="live-review-heading">Post-session record review</h2>
      <p>The server acknowledged the exact end barrier. Review affected records in the editor; any changes still require validation and Warden approval.</p>
      {reviewedCaptures.length ? <ul>{reviewedCaptures.map((event) => <li key={event.event_id}>
        <span>{event.event_type === "confirmed_fact" ? "Confirmed table fact" : "Unresolved question"} · record <code>{event.record_id}</code> · operation <code>{event.operation_id}</code></span>{" "}
        <button type="button" className="button-link" onClick={() => openRecord({ campaignId: campaign.campaign_id, sessionId: session.session_id, baseRevision: event.base_revision, operationId: event.operation_id, recordId: event.record_id })}>Review affected record</button>
      </li>)}</ul> : <p>No acknowledged captures have an affected record ID.</p>}
    </section>}
    {!session && <div className="card"><p>Start a live session at the current campaign head, or observe the active session in another tab.</p><button type="button" className="primary" disabled={busy} onClick={() => void start()}>{busy ? "Starting…" : "Start live session"}</button>{sessionId && <button type="button" disabled={busy} onClick={() => void observe()}>Retry session read</button>}</div>}
    {session && <>
      {endStatus && <p className="warning" role="status">End state: {session.mode === "ended_review_pending" ? "Ended - review pending" : endStatus}. The end action is locked locally; retry synchronization to confirm the server barrier.</p>}
      <div className="live-grid"><div className="live-main">
        <section className="card" aria-labelledby="capture-heading"><h2 id="capture-heading">Capture</h2><p>Capture is stored on this device before success is shown. Only confirmed table facts can ground later AI actions. Questions stay separate.</p>
          <form onSubmit={(event) => void capture(event)}><fieldset disabled={!canWrite || busy}><legend>Capture type</legend><label className="radio-label"><input type="radio" name="capture-type" checked={type === "confirmed_fact"} onChange={() => setType("confirmed_fact")} />Confirmed table fact</label><label className="radio-label"><input type="radio" name="capture-type" checked={type === "unresolved_question"} onChange={() => setType("unresolved_question")} />Unresolved question</label><label htmlFor="capture-text">What happened or remains unresolved?</label><textarea id="capture-text" value={captureText} onChange={(event) => setCaptureText(event.target.value)} rows={3} required /><label htmlFor="capture-record">Affected record ID, if known</label><input id="capture-record" value={recordId} onChange={(event) => setRecordId(event.target.value)} autoComplete="off" /><button className="primary" type="submit">Save capture</button></fieldset></form>
        </section>
        <section className="card" aria-labelledby="live-ai-heading"><h2 id="live-ai-heading">{action === "ask" ? "Ask" : action === "check" ? "Check" : "Generate"}</h2><form onSubmit={(event) => void runAi(event)}><fieldset disabled={!canWrite || busy || !aiReady}><legend>Live action</legend><label className="radio-label"><input type="radio" name="live-action" checked={action === "ask"} onChange={() => { setAction("ask"); setDraftContext(null); }} />Ask</label><label className="radio-label"><input type="radio" name="live-action" checked={action === "check"} onChange={() => { setAction("check"); setDraftContext(null); }} />Check</label><label className="radio-label"><input type="radio" name="live-action" checked={action === "generate"} onChange={() => setAction("generate")} />Generate</label><label htmlFor="live-prompt">{action === "ask" ? "Question" : action === "check" ? "Claim to check" : "Generation brief"}</label><textarea id="live-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={3} required /><button type="submit" disabled={!aiReady || !canWrite || busy}>Submit {action}</button></fieldset></form>
          {!aiReady && <p>Provider or consent is unavailable. Capture remains available.</p>}{generation && <><p className="badge badge--draft">Draft · {generation.status}</p><p>Source revision <code>{generation.source_revision}</code> · session <code>{generation.session_id}</code></p><section aria-labelledby="live-sources-heading"><h3 id="live-sources-heading">Sources</h3>{generation.sources.length ? <ol>{generation.sources.map((source) => <li key={source.source_id}><code>{source.source_id}</code> · {source.authority} · <code>{source.revision_id}</code><pre>{source.excerpt}</pre></li>)}</ol> : <p>No sources were returned.</p>}</section>{generation.status === "pending" && <button type="button" disabled={busy} onClick={() => void resumeDraft()}>Resume stream after event {sequence}</button>}</>}{draft && <div className="draft-output"><h3>Draft answer</h3><pre>{generation?.terminal_content ?? draft}</pre>{generation?.status === "complete" && draftContext?.recordId && <button type="button" className="button-link" onClick={() => openRecord(draftContext)}>Open affected record in editor with Draft provenance</button>}</div>}
        </section>
        <section className="card"><h2>End session</h2><p>Ending waits until the server acknowledges every capture required by the end barrier.</p><button type="button" className="danger" disabled={!canWrite || busy} onClick={() => void end()}>{endStatus ? "End intent saved" : "End session"}</button><button type="button" disabled={!controller || busy} onClick={() => void sync()}>Sync saved work</button></section>
      </div><aside className="live-rail" aria-label="Captured items"><section className="card"><h2>Confirmed table facts</h2>{facts.length ? <ol>{facts.map((item) => <li key={item.key}><p>{item.text}</p><p><strong>{syncStates[item.key] ?? item.state}</strong> · operation <code>{item.operationId}</code></p>{editLink(item)}</li>)}</ol> : <p>No confirmed table facts captured.</p>}</section><section className="card"><h2>Unresolved questions</h2><p>Excluded from live grounding.</p>{questions.length ? <ol>{questions.map((item) => <li key={item.key}><p>{item.text}</p><p><strong>{syncStates[item.key] ?? item.state}</strong> · operation <code>{item.operationId}</code></p>{editLink(item)}</li>)}</ol> : <p>No unresolved questions captured.</p>}</section></aside></div>
    </>}
    {error && <div className="error" role="alert">{error}</div>}
    <p className="announcer" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
  </section>;
}
