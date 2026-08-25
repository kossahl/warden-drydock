import { useEffect, useRef, useState, type FormEvent, type MouseEvent } from "react";
import { browserId, httpSliceApi, recordGenerationContext, type SliceApi } from "./api/client";
import { AuthorityBadge, RevisionStatus } from "./components/StatusPrimitives";
import type { CampaignRevisionView, GenerationView, ProposalView, ProviderReadiness, RecordView } from "./contracts/v2";

type BusyAction = "campaign" | "consent" | "ask" | "proposal" | "correct" | "reject" | "approve" | null;

const friendlyError = (error: unknown): string => {
  const code = error instanceof Error ? error.message : "request_failed";
  return code === "explicit_consent_required" ? "Grounded AI needs your explicit consent." : `Request failed (${code}).`;
};

export function ProposalWorkspace({ api = httpSliceApi, active = true, navigate }: { api?: SliceApi; active?: boolean; navigate?: (href: string) => void }) {
  const [readiness, setReadiness] = useState<ProviderReadiness | null>(null);
  const [campaign, setCampaign] = useState<CampaignRevisionView | null>(null);
  const [record, setRecord] = useState<RecordView | null>(null);
  const [generation, setGeneration] = useState<GenerationView | null>(null);
  const [streamDraft, setStreamDraft] = useState("");
  const [proposal, setProposal] = useState<ProposalView | null>(null);
  const [correctedContent, setCorrectedContent] = useState("");
  const [busy, setBusy] = useState<BusyAction>(null);
  const [error, setError] = useState<string | null>(null);
  const [streamInterrupted, setStreamInterrupted] = useState(false);
  const [lastObservedSequence, setLastObservedSequence] = useState(0);
  const [announcement, setAnnouncement] = useState("");
  const retries = useRef<Record<string, string>>({});
  const actionIds = useRef<Record<string, string>>({});
  const observedSequence = useRef(0);
  const mainRef = useRef<HTMLElement>(null);
  const wasActive = useRef(active);

  const retryKey = (action: string) => retries.current[action] ??= browserId(`idem_${action}`);
  const stableId = (action: string, prefix: string) => actionIds.current[action] ??= browserId(prefix);
  const finishAction = (message: string) => { setAnnouncement(message); setBusy(null); };
  const failAction = (failure: unknown) => { setError(friendlyError(failure)); setAnnouncement("The request failed."); setBusy(null); };

  useEffect(() => { if (active) void api.readiness().then(setReadiness).catch(failAction); }, [active, api]);
  useEffect(() => {
    if (active && !wasActive.current) mainRef.current?.focus();
    wasActive.current = active;
  }, [active]);

  async function createCampaign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy("campaign"); setError(null);
    const name = new FormData(event.currentTarget).get("campaign-name")?.toString().trim() ?? "";
    try {
      const created = await api.createCampaign(name, stableId("campaign", "campaign"), retryKey("campaign"));
      const first = created.records[0];
      const opened = await api.readRecord(created.campaign_id, created.viewed_revision.revision_id, first.record_id);
      setCampaign(created); setRecord(opened); retries.current = {}; actionIds.current = {}; finishAction(`Opened ${opened.name} at revision ${opened.revision_id}.`);
    } catch (failure) { failAction(failure); }
  }

  async function grantConsent() {
    if (!readiness?.consent_identity_digest) return;
    setBusy("consent"); setError(null);
    try { const value = await api.consent(readiness.consent_identity_digest, retryKey("consent")); setReadiness(value); finishAction("Grounded AI consent recorded."); }
    catch (failure) { failAction(failure); }
  }

  async function resumeStream(current: GenerationView) {
    setBusy("ask"); setError(null); setStreamInterrupted(false);
    try {
      const events = await api.resumeGeneration(current.generation_id, observedSequence.current);
      if (events.length) {
        observedSequence.current = Math.max(observedSequence.current, ...events.map((item) => item.sequence));
        setLastObservedSequence(observedSequence.current);
      }
      setStreamDraft((existing) => existing + events.map((item) => item.draft_fragment ?? "").join(""));
      const completed = await api.readGeneration(current.generation_id);
      setGeneration(completed);
      if (completed.status === "complete") { delete actionIds.current.ask; finishAction("Grounded Draft complete."); }
      else if (completed.status === "failed") { delete actionIds.current.ask; setStreamInterrupted(false); finishAction("The generation failed. Submit a new question to retry."); }
      else { setStreamInterrupted(true); finishAction("Stream paused. You can resume it."); }
    } catch (failure) { setStreamInterrupted(true); failAction(failure); }
  }

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!campaign || !record) return;
    setBusy("ask"); setError(null); setGeneration(null); setProposal(null); setStreamDraft(""); observedSequence.current = 0; setLastObservedSequence(0);
    const prompt = new FormData(event.currentTarget).get("question")?.toString().trim() ?? "";
    try {
      const context = await recordGenerationContext(record);
      const started = await api.startGeneration(campaign.campaign_id, record.revision_id, "ask", prompt, stableId("ask", "generation"), context);
      setGeneration(started); setAnnouncement("Sources pinned. Grounded Draft is streaming.");
      await resumeStream(started);
    } catch (failure) { failAction(failure); }
  }

  async function createProposal() {
    if (!generation || !record) return;
    setBusy("proposal"); setError(null);
    try { const value = await api.createProposal(generation, record.record_id, stableId("proposal", "proposal"), retryKey("proposal")); delete actionIds.current.proposal; setProposal(value); setCorrectedContent(value.exact_diff[0].after_content); finishAction("Proposal created. Review the exact diff before approval."); }
    catch (failure) { failAction(failure); }
  }

  async function correctProposal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!proposal) return;
    setBusy("correct"); setError(null);
    try { const value = await api.correctProposal(proposal, correctedContent, retryKey(`correct_${proposal.proposal_version}`)); setProposal(value); setCorrectedContent(value.exact_diff[0].after_content); finishAction(`Proposal corrected to version ${value.proposal_version}.`); }
    catch (failure) { failAction(failure); }
  }

  async function rejectProposal() {
    if (!proposal) return;
    setBusy("reject"); setError(null);
    try { const value = await api.rejectProposal(proposal, retryKey(`reject_${proposal.proposal_version}`)); setProposal(value); finishAction("Proposal rejected. Canon was not changed."); }
    catch (failure) { failAction(failure); }
  }

  async function approveProposal() {
    if (!proposal || !campaign) return;
    setBusy("approve"); setError(null);
    try {
      const result = await api.approveProposal(proposal, campaign.head_revision, retryKey(`approve_${proposal.proposal_version}`));
      setProposal(result.proposal);
      if (result.outcome === "conflict") { finishAction("Conflict. The proposal is preserved and was not published."); return; }
      const revision = await api.readRevision(campaign.campaign_id, result.published_revision.revision_id);
      const opened = await api.readRecord(revision.campaign_id, revision.viewed_revision.revision_id, revision.records[0].record_id);
      setCampaign(revision); setRecord(opened); finishAction(`Published and opened validated revision ${opened.revision_id}.`);
    } catch (failure) { failAction(failure); }
  }

  const currentDraft = generation?.terminal_content ?? streamDraft;
  const disabled = busy !== null;
  const providerStatus = !readiness
    ? "Checking"
    : !readiness.provider_configured
      ? "Setup required"
      : !readiness.provider_available
        ? "Unavailable"
        : !readiness.consent_current
          ? "Consent required"
          : readiness.ai_available ? "Ready" : "Unavailable";

  return (
    <div className="app-shell" hidden={!active}>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="banner">
        <div><p className="eyebrow">Local Warden workspace</p><strong>Warden Drydock</strong></div>
        <span role="status">Provider: {providerStatus}</span>
      </header>
      <main id="main-content" tabIndex={-1} ref={mainRef}>
        {!campaign ? (
          <section className="card narrow" aria-labelledby="create-heading">
            <p className="eyebrow">Deterministic setup</p><h1 id="create-heading">Create a campaign</h1>
            <p>Create one local synthetic campaign. Import is not part of this pilot.</p>
            <form onSubmit={createCampaign}>
              <label htmlFor="campaign-name">Campaign name</label>
              <input id="campaign-name" name="campaign-name" required maxLength={120} defaultValue="Synthetic Campaign" />
              <button disabled={disabled} type="submit">{busy === "campaign" ? "Creating…" : "Create campaign"}</button>
            </form>
          </section>
        ) : record ? (
          <>
            <aside aria-label="Revision and authority" className="authority-strip">
              <RevisionStatus viewed={campaign.viewed_revision} head={campaign.head_revision} />
              <AuthorityBadge authority={record.authority} />
            </aside>
            <p><a href={`/campaigns/${encodeURIComponent(campaign.campaign_id)}?revision=${encodeURIComponent(campaign.viewed_revision.revision_id)}`} onClick={(event: MouseEvent<HTMLAnchorElement>) => { if (!navigate || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(event.currentTarget.getAttribute("href")!); }}>Browse Campaign Atlas</a></p>
            <p className="eyebrow">{campaign.campaign_name} · {record.record_type}</p>
            <h1>{record.name}</h1>
            <p className="revision-id">Source revision <code>{record.revision_id}</code></p>
            <section className="card canon" aria-labelledby="record-heading"><div className="section-title"><h2 id="record-heading">Revision record</h2><AuthorityBadge authority={record.authority} /></div><pre>{record.content}</pre></section>

            {readiness?.provider_configured && readiness.provider_available && !readiness.consent_current && (
              <section className="card consent" aria-labelledby="consent-heading"><h2 id="consent-heading">Grounded AI consent</h2><p>AI use is optional. Generated text stays Draft until you approve an exact proposal.</p><button type="button" disabled={disabled || !readiness?.provider_available} onClick={grantConsent}>{busy === "consent" ? "Recording consent…" : "Allow grounded AI"}</button></section>
            )}

            <section className="card ask" aria-labelledby="ask-heading">
              <h2 id="ask-heading">Ask about this revision</h2>
              <form onSubmit={ask}><label htmlFor="question">Grounded question</label><textarea id="question" name="question" required rows={3} defaultValue="What is this campaign called?" /><button disabled={disabled || !readiness?.ai_available} type="submit">{busy === "ask" ? "Grounding answer…" : "Ask grounded question"}</button></form>
              {generation && <p className="stream-state" role="status">Stream {generation.status}. Last event {generation.last_sequence}.</p>}
              {streamInterrupted && generation && <button type="button" disabled={disabled} onClick={() => void resumeStream(generation)}>Resume stream after event {lastObservedSequence}</button>}
            </section>

            {generation && (
              <section className="card sources" aria-labelledby="sources-heading"><h2 id="sources-heading">Sources</h2><p>Action <code>{generation.action}</code>. Context <code>{generation.context.scope === "record" ? `record:${generation.context.record_id}` : "campaign"}</code>. Session <code>{generation.session_id ?? "none"}</code>.</p>{generation.context.scope === "record" && <p>Bound content digest <code>{generation.context.content_digest}</code></p>}<p>Source set <code>{generation.source_set_digest}</code></p><ol>{generation.sources.map((source) => <li key={source.source_id}><code>{source.source_id}</code> <AuthorityBadge authority={source.authority} /> Revision <code>{source.revision_id}</code><details><summary>Inspect excerpt</summary><pre>{source.excerpt}</pre></details></li>)}</ol></section>
            )}

            {currentDraft && (
              <section className="card draft" aria-labelledby="draft-heading"><div className="section-title"><h2 id="draft-heading">Grounded Draft</h2><AuthorityBadge authority="draft" /></div><p>This text is not canon.</p><pre>{currentDraft}</pre>{generation?.status === "complete" && !proposal && <button type="button" disabled={disabled} onClick={createProposal}>{busy === "proposal" ? "Creating proposal…" : "Create proposal"}</button>}</section>
            )}

            {proposal && (
              <section className={`card proposal proposal--${proposal.status}`} aria-labelledby="proposal-heading">
                <div className="section-title"><h2 id="proposal-heading">Proposal version {proposal.proposal_version}</h2><AuthorityBadge authority="proposal" /></div>
                <p>Status: <strong>{proposal.status}</strong>. Base revision <code>{proposal.base_revision}</code>.</p>
                {proposal.status === "conflict" && <div className="conflict" role="alert"><h3>Conflict</h3><p>The campaign head changed. This proposal is preserved and has not been published.</p></div>}
                <h3>Exact diff</h3><p>Diff digest <code>{proposal.diff_digest}</code></p>
                <div className="diff" role="region" aria-label="Complete before and after content"><article><h4>Before · {proposal.exact_diff[0].from_authority}</h4><pre>{proposal.exact_diff[0].before_content}</pre></article><article><h4>After · {proposal.exact_diff[0].to_authority}</h4><pre>{proposal.exact_diff[0].after_content}</pre></article></div>
                {(proposal.status === "draft" || proposal.status === "conflict") && <form onSubmit={correctProposal}><label htmlFor="corrected-content">Complete corrected after content</label><textarea id="corrected-content" rows={10} value={correctedContent} onChange={(event) => setCorrectedContent(event.target.value)} /><button disabled={disabled} type="submit">{busy === "correct" ? "Correcting…" : "Create corrected version"}</button></form>}
                {proposal.status === "draft" && <div className="actions"><button className="danger" type="button" disabled={disabled} onClick={rejectProposal}>{busy === "reject" ? "Rejecting…" : "Reject proposal"}</button><button className="primary" type="button" disabled={disabled} onClick={approveProposal}>{busy === "approve" ? "Approving exact diff…" : "Approve exact diff"}</button></div>}
              </section>
            )}
          </>
        ) : null}
        {error && <div className="error" role="alert"><strong>Action failed</strong><p>{error}</p></div>}
      </main>
      <div className="announcer" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
    </div>
  );
}
