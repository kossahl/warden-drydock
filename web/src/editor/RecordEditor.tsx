import { recordTypes, recordDefinitions, newAdapterRecord, relationships, connectionStates } from "./adapterDefinition";
import { httpAtlasApi } from "../api/atlasClient";
import type { AtlasRecordSummary } from "../contracts/v2";
import { useEffect, useRef, useState } from "react";
import { httpEditorApi, nextConnectionId, type EditorConnection, type EditorField, type EditorProposal, type EditorRecord, type EditorRecordView, type EditorRemovalImpact, type EditorSection, type RevisionRef } from "./editorClient";

const statuses = ["idea", "draft", "review", "canon", "revealed", "archived", "accepted"];
const authority = (status: string) => status === "canon" || status === "revealed" ? status : "preparation";
const clone = (record: EditorRecord): EditorRecord => ({ ...record, fields: record.fields.map((item) => ({ ...item })), sections: record.sections.map((item) => ({ ...item })), connections: record.connections.map((item) => ({ ...item })) });
const reviewedProposalCandidate = (proposal: EditorProposal): EditorRecord | null => {
  const card = proposal.diff.cards.find((item) => (item.kind === "record_created" || item.kind === "record_updated") && item.after !== null && typeof item.after === "object" && !Array.isArray(item.after));
  return card ? clone(card.after as EditorRecord) : null;
};
const publicId = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : "request_failed";
const errorCategory = (reason: unknown) => reason && typeof reason === "object" && "category" in reason ? String((reason as { category?: unknown }).category ?? "") : "";
const staleCategories = ["stale_revision", "workflow_conflict", "stale_record_digest"];
const atlasRevision = (revision: RevisionRef) => ({ revision_id: revision.revision_id, revision_ordinal: revision.ordinal, tree_digest: revision.tree_digest });
const editorProposalLocation = (proposal: EditorProposal | null) => {
  const url = new URL(window.location.href);
  if (proposal) {
    url.searchParams.set("proposal", proposal.proposal_id);
    url.searchParams.set("version", String(proposal.proposal_version));
  } else {
    url.searchParams.delete("proposal");
    url.searchParams.delete("version");
  }
  return `${url.pathname}${url.search}`;
};

function RecordPicker({ campaignId, revision, label, value, onChange, error }: { campaignId: string; revision: RevisionRef; label: string; value: string; onChange: (recordId: string) => void; error?: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(value);
  const [choices, setChoices] = useState<ReadonlyArray<AtlasRecordSummary>>([]);
  const [selected, setSelected] = useState<AtlasRecordSummary | null>(null);
  const [pending, setPending] = useState(false);
  const [loadError, setLoadError] = useState("");
  const search = async (term: string) => {
    setPending(true); setLoadError("");
    try {
      const result = await httpAtlasApi.records(campaignId, { ...atlasRevision(revision), q: term.trim(), types: [], authorities: [], statuses: [] });
      setChoices(result.items);
    } catch (reason) {
      setChoices([]); setLoadError(`Record search unavailable (${errorText(reason)}).`);
    } finally { setPending(false); }
  };
  const openPicker = () => { setQuery(value); setOpen(true); void search(value); };
  const inputId = label.replace(/[^a-z0-9]+/gi, "-");
  return <div className="record-picker"><p id={`${inputId}-selected`}>{selected && selected.record_id === value ? <>Selected: {selected.name} <span>({selected.record_type})</span> · <code>{selected.record_id}</code></> : value ? <>Selected record ID: <code>{value}</code></> : "No record selected."}</p><button type="button" aria-label={`${label}: ${value ? "change selected record" : "choose existing record"}`} aria-haspopup="dialog" aria-controls={`${inputId}-dialog`} onClick={openPicker}>{value ? "Change selected record" : "Choose existing record"}</button>{error && <span className="error" role="alert">{error}</span>}{open && <div className="record-picker-dialog" role="dialog" aria-modal="true" aria-labelledby={`${inputId}-dialog-heading`} id={`${inputId}-dialog`}><h4 id={`${inputId}-dialog-heading`}>Choose an existing record</h4><p>Search by displayed name, record type, or stable record ID.</p><form onSubmit={(event) => { event.preventDefault(); void search(query); }}><label htmlFor={`${inputId}-search`}>Search existing records</label><input id={`${inputId}-search`} value={query} onChange={(event) => setQuery(event.target.value)} /><button type="submit" disabled={pending}>Search</button></form>{pending && <p role="status">Searching existing records.</p>}{loadError && <p className="error" role="alert">{loadError}</p>}{!pending && !loadError && <>{choices.length ? <ul role="listbox" aria-label="Existing records">{choices.map((choice) => <li key={choice.record_id}><button type="button" role="option" onClick={() => { onChange(choice.record_id); setSelected(choice); setOpen(false); }}>{choice.name} <span>({choice.record_type})</span> · <code>{choice.record_id}</code></button></li>)}</ul> : <p>No existing records match this search.</p>}</>}<button type="button" onClick={() => setOpen(false)}>Cancel</button></div>}</div>;
}

export function RecordEditor({ campaignId, revisionId, recordId, proposalId, proposalVersion, navigate }: { campaignId: string; revisionId: string; recordId: string; proposalId?: string | null; proposalVersion?: number | null; navigate?: (href: string) => void }) {
  const isCreate = recordId === "__new__";
  const [view, setView] = useState<EditorRecordView | null>(null);
  const [draft, setDraft] = useState<EditorRecord | null>(null);
  const [proposal, setProposal] = useState<EditorProposal | null>(null);
  const [impact, setImpact] = useState<EditorRemovalImpact | null>(null);
  const [resolutions, setResolutions] = useState<Array<Record<string, unknown>>>([]);
  const [mode, setMode] = useState<"edit" | "remove">("edit");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [approvalDialog, setApprovalDialog] = useState<"approve" | "reject" | null>(null);
  const [wardenConfirmed, setWardenConfirmed] = useState(false);
  const [rejectionReason, setRejectionReason] = useState("review_rejected");
  const [conflict, setConflict] = useState(false);
  const [correctionMode, setCorrectionMode] = useState(false);
  const correctionDraft = useRef<EditorRecord | null>(null);
  const correctionResolutions = useRef<Array<Record<string, unknown>> | null>(null);
  const correctionView = useRef<EditorRecordView | null>(null);
  const correctionImpact = useRef<EditorRemovalImpact | null>(null);
  const correctionBase = useRef<{ view: EditorRecordView; impact: EditorRemovalImpact | null } | null>(null);
  const correctionRequest = useRef(0);
  const proposalRequest = useRef(0);
  const errorHeading = useRef<HTMLHeadingElement>(null);
  const dialogHeading = useRef<HTMLHeadingElement>(null);
  const focusEditorError = useRef(false);
  const loadRequest = useRef<{ sequence: number; campaignId: string; revisionId: string; recordId: string } | null>(null);
  const editorIdentity = `${campaignId}\u0000${revisionId}\u0000${recordId}`;
  const editorIdentityRef = useRef(editorIdentity);
  editorIdentityRef.current = editorIdentity;
  const proposalIdentity = proposal ? `${proposal.proposal_id}\u0000${proposal.proposal_version}` : null;
  const proposalIdentityRef = useRef<string | null>(proposalIdentity);
  proposalIdentityRef.current = proposalIdentity;

  const load = (sourceRevisionId = revisionId) => {
    correctionRequest.current += 1;
    proposalRequest.current += 1;
    setBusy(false); setError(""); setMessage(""); setConflict(false); setProposal(null); setImpact(null); setCorrectionMode(false); correctionDraft.current = null; correctionResolutions.current = null; correctionView.current = null; correctionImpact.current = null; correctionBase.current = null;
    const sourceRecordId = isCreate ? "campaign-main" : recordId;
    const request = { sequence: (loadRequest.current?.sequence ?? 0) + 1, campaignId, revisionId: sourceRevisionId, recordId: sourceRecordId };
    loadRequest.current = request;
    const isCurrentRequest = () => {
      const current = loadRequest.current;
      return current !== null
        && current.sequence === request.sequence
        && current.campaignId === request.campaignId
        && current.revisionId === request.revisionId
        && current.recordId === request.recordId;
    };
    void httpEditorApi.read(campaignId, sourceRevisionId, sourceRecordId).then((value) => {
      if (!isCurrentRequest()) return;
      setView(value);
      setDraft(isCreate ? newAdapterRecord() : clone(value.record));
    }).catch((reason: unknown) => {
      if (!isCurrentRequest()) return;
      focusEditorError.current = !document.activeElement?.closest("#atlas-content"); setError(`Editor unavailable (${errorText(reason)}).`);
    });
  };
  useEffect(() => load(), [campaignId, revisionId, recordId]);
  useEffect(() => {
    if (!proposalId || !proposalVersion) return;
    let active = true;
    void httpEditorApi.proposal(proposalId, proposalVersion).then((value) => {
      if (!active) return;
      const binding = value.record_bindings[0];
      const matchesEditor = value.campaign_id === campaignId
        && (isCreate ? value.mutation_kind === "create" : binding?.record_id === recordId);
      if (!matchesEditor) throw new Error("proposal_binding_mismatch");
      setProposal(value);
      setMessage("Submitted proposal restored for review.");
    }).catch((reason: unknown) => {
      if (!active) return;
      setError(`Submitted proposal could not be restored (${errorText(reason)}).`);
    });
    return () => { active = false; };
  }, [campaignId, isCreate, proposalId, proposalVersion, recordId]);
  useEffect(() => {
    if (!error || !focusEditorError.current) return;
    focusEditorError.current = false;
    if (document.activeElement?.closest("#atlas-content") && !document.activeElement?.closest(".editor")) return;
    errorHeading.current?.focus();
  }, [error]);
  useEffect(() => { setWardenConfirmed(false); if (approvalDialog) dialogHeading.current?.focus(); }, [approvalDialog, proposal]);
  useEffect(() => {
    const first = Object.keys(fieldErrors)[0];
    if (!first || !document.activeElement?.closest(".editor")) return;
    const id = first === "record_id" ? "editor-record-id" : first === "displayed_name" ? "editor-name" : first.startsWith("field-") ? `editor-field-${first.slice(6)}` : first.startsWith("section-") ? `editor-section-${first.slice(8)}` : first.startsWith("connection-") ? `connection-target-${first.slice(11)}` : null;
    if (id) document.getElementById(id)?.focus();
  }, [fieldErrors]);

  const update = (next: Partial<EditorRecord>) => setDraft((current) => current ? { ...current, ...next } : current);
  const validate = () => {
    if (!draft) return false;
    const next: Record<string, string> = {};
    if (!/^[a-z0-9][a-z0-9-]*$/.test(draft.record_id)) next.record_id = "Use lowercase letters, numbers, and hyphens.";
    if (!draft.displayed_name.trim()) next.displayed_name = "Displayed name is required.";
    draft.fields.forEach((field) => { if (!/^[a-z0-9][a-z0-9_-]*$/.test(field.field_id)) next[`field-${field.field_id}`] = "Field ID is invalid."; });
    draft.sections.forEach((section) => { if (!/^[a-z0-9][a-z0-9-]*$/.test(section.section_id)) next[`section-${section.section_id}`] = "Section ID is invalid."; });
    const connectionIds = new Set<string>();
    draft.connections.forEach((connection) => {
      if (connection.connection_id.length < 3 || !publicId.test(connection.connection_id)) next[`connection-${connection.connection_id}`] = "Connection ID must use lowercase public ID syntax.";
      if (connectionIds.has(connection.connection_id)) next[`connection-${connection.connection_id}`] = "Connection IDs must be unique.";
      connectionIds.add(connection.connection_id);
      if (!connection.target_record_id.trim()) next[`connection-${connection.connection_id}`] = "A connection target is required.";
    });
    setFieldErrors(next); return Object.keys(next).length === 0;
  };
  const removalReady = !!impact && impact.incoming_references.every((reference) => {
    const resolution = resolutions.find((item) => item.reference_id === reference.reference_id);
    const replacement = resolution?.replacement_target_record_id;
    return resolution?.action === "remove_reference"
      || (resolution?.action === "redirect" && typeof replacement === "string" && replacement.length > 0)
      || (resolution?.action === "accept_unresolved" && reference.permitted_unresolved === true);
  });
  const startRemove = async () => {
    if (!view || !draft || !view.editable || !validate()) return;
    setBusy(true); setError(""); setMessage(""); setConflict(false);
    try { const value = await httpEditorApi.impact(campaignId, view.head_revision.revision_id, draft.record_id); setImpact(value); setMode("remove"); setResolutions(value.incoming_references.map((reference) => ({ reference_id: reference.reference_id, action: "", replacement_target_record_id: null }))); setMessage("Resolve every incoming typed connection before submitting removal."); }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`Removal impact unavailable (${errorText(reason)}).`); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (mode === "remove" && !removalReady) return;
    if (!view || !draft || !view.editable || !validate()) return;
    const request = { sequence: proposalRequest.current + 1, editorIdentity };
    proposalRequest.current = request.sequence;
    const isCurrentRequest = () => proposalRequest.current === request.sequence && editorIdentityRef.current === request.editorIdentity;
    setBusy(true); setError(""); setMessage(""); setConflict(false);
    try {
      const value = await httpEditorApi.propose(isCreate ? "create" : mode, campaignId, view.head_revision, draft, view.editor_workflow_version, resolutions, impact ?? undefined);
      if (!isCurrentRequest()) return;
      setProposal(value); navigate?.(editorProposalLocation(value)); setCorrectionMode(false); setMessage("Exact proposal loaded for review. The current head is unchanged.");
    } catch (reason) {
      if (!isCurrentRequest()) return;
      setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`Proposal was not created (${errorText(reason)}).`);
    } finally { if (isCurrentRequest()) setBusy(false); }
  };
  const submitDecision = async () => {
    if (!proposal || !approvalDialog || correctionMode || (approvalDialog === "approve" && !wardenConfirmed)) return;
    const approving = approvalDialog === "approve"; setBusy(true); setError(""); setConflict(false);
    try { const result = approving ? await httpEditorApi.approve(proposal, wardenConfirmed) : await httpEditorApi.reject(proposal, rejectionReason); setApprovalDialog(null); if (approving) { setMessage("Proposal approved and published."); window.dispatchEvent(new Event("drydock:campaign-mutated")); const revision = result.published_revision as RevisionRef | undefined; const createdRecordId = proposal.mutation_kind === "create" ? proposal.record_bindings[0]?.record_id : undefined; if (revision && navigate) navigate(createdRecordId ? `/campaigns/${encodeURIComponent(campaignId)}/records/${encodeURIComponent(createdRecordId)}?revision=${encodeURIComponent(revision.revision_id)}` : `/campaigns/${encodeURIComponent(campaignId)}?revision=${encodeURIComponent(revision.revision_id)}`); } else { setView((current) => current ? { ...current, editor_workflow_version: result.editor_workflow_version as number } : current); setProposal(null); navigate?.(editorProposalLocation(null)); setCorrectionMode(false); setMessage("Proposal rejected. No campaign revision changed."); } }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`${approving ? "Approval" : "Rejection"} blocked (${errorText(reason)}). Refresh and review the current head.`); }
    finally { setBusy(false); }
  };
  const startCorrection = async () => {
    if (!proposal || busy) return;
    const request = {
      sequence: correctionRequest.current + 1,
      editorIdentity,
      proposalIdentity: `${proposal.proposal_id}\u0000${proposal.proposal_version}`,
    };
    correctionRequest.current = request.sequence;
    const isCurrentRequest = () => correctionRequest.current === request.sequence
      && editorIdentityRef.current === request.editorIdentity
      && proposalIdentityRef.current === request.proposalIdentity;
    correctionDraft.current = draft ? clone(draft) : null;
    correctionResolutions.current = resolutions.map((resolution) => ({ ...resolution }));
    correctionView.current = view;
    correctionImpact.current = impact;
    setBusy(true); setFieldErrors({}); setError(""); setConflict(false);
    try {
      const proposalRecordId = proposal.record_bindings[0]?.record_id;
      const sourceRecordId = proposal.mutation_kind === "create" ? "campaign-main" : proposalRecordId ?? draft?.record_id ?? recordId;
      const base = await httpEditorApi.read(campaignId, proposal.base_revision.revision_id, sourceRecordId);
      if (!isCurrentRequest()) return;
      const currentHead = base.viewed_revision.revision_id === base.head_revision.revision_id
        ? base
        : await httpEditorApi.read(campaignId, base.head_revision.revision_id, sourceRecordId);
      if (!isCurrentRequest()) return;
      const currentImpact = proposal.mutation_kind === "remove"
        ? await httpEditorApi.impact(campaignId, currentHead.head_revision.revision_id, sourceRecordId)
        : null;
      if (!isCurrentRequest()) return;
      correctionBase.current = { view: currentHead, impact: currentImpact };
      const rebasingStaleProposal = currentHead.head_revision.revision_id !== proposal.base_revision.revision_id;
      if (proposal.mutation_kind === "create" || !rebasingStaleProposal) {
        const candidate = reviewedProposalCandidate(proposal);
        if (candidate) setDraft({ ...candidate, ...(proposalRecordId ? { record_id: proposalRecordId } : {}) });
        else setDraft((current) => current && proposalRecordId ? { ...current, record_id: proposalRecordId } : current);
      } else {
        setDraft(clone(currentHead.record));
      }
      if (currentImpact) {
        setResolutions(currentImpact.incoming_references.map((reference) => resolutions.find((item) => item.reference_id === reference.reference_id) ?? { reference_id: reference.reference_id, action: "", replacement_target_record_id: null }));
      }
      setView(currentHead); setImpact(currentImpact); setCorrectionMode(true); setMessage("Correction mode: edit the candidate from the current head, then submit a new proposal version.");
    } catch (reason) {
      if (!isCurrentRequest()) return;
      correctionDraft.current = null; correctionResolutions.current = null; correctionView.current = null; correctionImpact.current = null;
      setError(`Correction could not start (${errorText(reason)}). Reload the current head and try again.`); focusEditorError.current = true;
    } finally { if (isCurrentRequest()) setBusy(false); }
  };
  const cancelCorrection = () => {
    if (!correctionMode) return;
    if (correctionDraft.current) setDraft(correctionDraft.current);
    if (correctionResolutions.current) setResolutions(correctionResolutions.current);
    if (correctionView.current) setView(correctionView.current);
    setImpact(correctionImpact.current);
    correctionDraft.current = null; correctionResolutions.current = null;
    correctionView.current = null; correctionImpact.current = null; correctionBase.current = null;
    setFieldErrors({}); setCorrectionMode(false); setError(""); setConflict(false); setMessage("Correction canceled. The original proposal remains under review.");
  };
  const submitCorrection = async () => {
    if (proposal?.mutation_kind === "remove" && !removalReady) return;
    if (!proposal || !draft || !correctionMode || (proposal.mutation_kind !== "remove" && !validate())) return;
    setBusy(true); setError(""); setConflict(false);
    try {
      const proposalRecordId = proposal.record_bindings[0]?.record_id;
      const correctedDraft = proposal.mutation_kind === "create" && proposalRecordId
        ? { ...draft, record_id: proposalRecordId }
        : draft;
      const correctionBinding = correctionBase.current;
      if (!correctionBinding) throw new Error("correction_base_required");
      const currentHead = correctionBinding.view;
      const currentImpact = correctionBinding.impact ?? undefined;
      const value = await httpEditorApi.correct(
        proposal, proposal.mutation_kind === "remove" ? null : correctedDraft, resolutions,
        currentHead.head_revision, currentHead.editor_workflow_version, proposal.mutation_kind === "create" ? undefined : currentHead.record.content_digest,
        currentImpact,
      );
      setView(currentHead); setDraft(correctedDraft); setProposal(value); navigate?.(editorProposalLocation(value)); setImpact(currentImpact ?? null); setCorrectionMode(false); correctionDraft.current = null; correctionResolutions.current = null; correctionView.current = null; correctionImpact.current = null; correctionBase.current = null; setMessage("Correction created as a new immutable proposal version.");
    }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`Correction blocked (${errorText(reason)}). Reload the current head and rebase the fields.`); }
    finally { setBusy(false); }
  };
  const openCurrentHead = async () => {
    setBusy(true); setError("");
    try {
      const current = (await httpAtlasApi.campaigns()).campaigns.find((item) => item.campaign_id === campaignId);
      if (!current) throw new Error("campaign_unavailable");
      // Document navigation refreshes Atlas's campaign cache and same-revision editor state.
      load(current.head_revision.revision_id);
      navigate?.(`/campaigns/${encodeURIComponent(campaignId)}/records/${encodeURIComponent(recordId)}?revision=${encodeURIComponent(current.head_revision.revision_id)}`);
    } catch (reason) { focusEditorError.current = true; setError(`Current head unavailable (${errorText(reason)}).`); }
    finally { setBusy(false); }
  };

  if (approvalDialog && proposal) { const audienceBroadens = proposal.diff.visibility_changes.some((change) => change.audience_broadens === true); return <dialog open className="editor-dialog" aria-modal="true" aria-labelledby="editor-dialog-heading" onKeyDown={(event) => { if (event.key === "Escape") setApprovalDialog(null); }}><section role="document"><h2 id="editor-dialog-heading" ref={dialogHeading} tabIndex={-1}>{approvalDialog === "approve" ? "Approve exact proposal" : "Reject proposal"}</h2><p>Proposal <code>{proposal.proposal_id}</code>, version {proposal.proposal_version}. Base revision <code>{proposal.base_revision.revision_id}</code>.</p>{approvalDialog === "approve" ? <><p>Validation: {proposal.validation.status}. Affected records: {proposal.diff.affected_record_count}. Removed records: {proposal.diff.cards.filter((card) => card.kind === "record_removed").length}.</p><ul>{proposal.diff.cards.map((card) => <li key={card.change_id}>{String((card.after as EditorRecord | undefined)?.displayed_name ?? (card.before as EditorRecord | undefined)?.displayed_name ?? card.subject_record_id)}</li>)}</ul><p>Authority changes: {proposal.diff.authority_changes.length}. Visibility changes: {proposal.diff.visibility_changes.length}.</p>{audienceBroadens && <div className="warning" role="alert"><strong>Warning: this proposal broadens audience visibility.</strong><p>Review the affected records and confirm that the new audience may see them before publishing.</p></div>}<pre>{JSON.stringify({ authority: proposal.authority_outcome, visibility: proposal.visibility_outcome }, null, 2)}</pre><label><input type="checkbox" checked={wardenConfirmed} onChange={(event) => setWardenConfirmed(event.target.checked)} />I confirm the exact proposal, validation, and authority and visibility changes.</label></> : <label htmlFor="editor-rejection-reason">Reason code<input id="editor-rejection-reason" value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)} /></label>}<div className="actions"><button type="button" disabled={busy} onClick={() => setApprovalDialog(null)}>Cancel</button><button type="button" className="primary" disabled={busy || (approvalDialog === "approve" && !wardenConfirmed) || (approvalDialog === "reject" && !/^[a-z][a-z0-9_]+$/.test(rejectionReason))} onClick={() => void submitDecision()}>{approvalDialog === "approve" ? "Approve and publish exact proposal" : "Reject exact proposal"}</button></div></section></dialog>; }
  if (error && !view) return <section className="card editor" role="alert" aria-labelledby="editor-error-heading"><h2 id="editor-error-heading" ref={errorHeading} tabIndex={-1}>Record editor</h2><p>{error}</p><button type="button" onClick={() => load()}>Retry</button></section>;
  if (!view || !draft) return <section className="card editor" aria-busy="true"><h2>Record editor</h2><p role="status">Loading structured record.</p></section>;
  if (!view.editable) return <section className="card editor" aria-labelledby="editor-heading"><h2 id="editor-heading">Record editor</h2><p>Historical revisions are read-only. Open the current head to propose a change.</p>{navigate && <button type="button" onClick={openCurrentHead}>Open current head</button>}</section>;

  const invalid = (key: string) => fieldErrors[key];
  const setField = (index: number, field: EditorField) => update({ fields: draft.fields.map((item, itemIndex) => itemIndex === index ? field : item) });
  const setSection = (index: number, section: EditorSection) => update({ sections: draft.sections.map((item, itemIndex) => itemIndex === index ? section : item) });
  const locked = busy || (!!proposal && !correctionMode);
  return <section className="card editor" aria-labelledby="editor-heading"><div className="section-title"><h2 id="editor-heading">{isCreate ? "Create record" : "Edit record"}</h2><span role="status">Head · workflow {view.editor_workflow_version}</span></div><p>Changes create a typed proposal. Approval is required before the campaign head changes.</p>{error && <div className="error editor-error" role="alert" aria-labelledby="editor-error-heading"><h3 id="editor-error-heading" ref={errorHeading} tabIndex={-1}>Editor error</h3><p>{error}</p></div>}{message && <p role="status" aria-live="polite">{message}</p>}{conflict && <aside className="editor-conflict" role="alert" aria-labelledby="editor-conflict-heading"><h3 id="editor-conflict-heading">Head changed; rebase required</h3><p>This proposal is bound to an older revision or workflow. Reload the current head before retrying.</p>{navigate && <button type="button" onClick={openCurrentHead}>Reload current head</button>}</aside>}
    <fieldset disabled={locked}><legend>Record details</legend><label htmlFor="editor-record-id">Record ID</label><input id="editor-record-id" value={draft.record_id} readOnly={!isCreate || (!!proposal && correctionMode)} onChange={(event) => update({ record_id: event.target.value })} aria-invalid={!!invalid("record_id")} aria-describedby={invalid("record_id") ? "editor-record-id-error" : undefined} />{invalid("record_id") && <span id="editor-record-id-error" className="error">{invalid("record_id")}</span>}<label htmlFor="editor-name">Displayed name</label><input id="editor-name" value={draft.displayed_name} onChange={(event) => update({ displayed_name: event.target.value })} aria-invalid={!!invalid("displayed_name")} aria-describedby={invalid("displayed_name") ? "editor-name-error" : undefined} />{invalid("displayed_name") && <span id="editor-name-error" className="error">{invalid("displayed_name")}</span>}<label htmlFor="editor-type">Record type</label><select id="editor-type" value={draft.record_type} disabled={!isCreate || !!proposal} onChange={(event) => setDraft(newAdapterRecord(event.target.value, draft.record_id, draft.displayed_name))}>{(isCreate ? recordTypes : [draft.record_type]).map((type) => <option key={type} value={type}>{type}</option>)}</select><label htmlFor="editor-status">Status</label><select id="editor-status" value={draft.status} onChange={(event) => update({ status: event.target.value, authority: authority(event.target.value) as EditorRecord["authority"] })}>{statuses.map((value) => <option key={value} value={value}>{value}</option>)}</select><p>Authority: <strong>{authority(draft.status)}</strong> (derived from status)</p><label htmlFor="editor-visibility">Visibility</label><select id="editor-visibility" value={draft.visibility.audience} onChange={(event) => update({ visibility: event.target.value === "warden" ? { audience: "warden", warden_only: true } : { audience: event.target.value as "players" | "shared", warden_only: false } })}><option value="warden">Warden only</option><option value="shared">Shared</option><option value="players">Players</option></select></fieldset>
    <fieldset disabled={locked}><legend>Fields</legend>{draft.fields.map((field, index) => <div key={field.field_id}><label htmlFor={`editor-field-${field.field_id}`}>{field.field_id}</label><input id={`editor-field-${field.field_id}`} value={String(field.value ?? "")} readOnly={!recordDefinitions[draft.record_type]?.fields.includes(field.field_id)} onChange={(event) => setField(index, { ...field, value: event.target.value })} aria-invalid={!!invalid(`field-${field.field_id}`)} />{invalid(`field-${field.field_id}`) && <span className="error">{invalid(`field-${field.field_id}`)}</span>}</div>)}</fieldset>
    <fieldset disabled={locked}><legend>Content sections</legend>{draft.sections.map((section, index) => <div key={section.section_id}><label htmlFor={`editor-section-${section.section_id}`}>{section.section_id}</label><textarea id={`editor-section-${section.section_id}`} rows={5} readOnly={!recordDefinitions[draft.record_type]?.sections.some((item) => item.id === section.section_id)} value={section.body} onChange={(event) => setSection(index, { ...section, body: event.target.value })} aria-invalid={!!invalid(`section-${section.section_id}`)} />{invalid(`section-${section.section_id}`) && <span className="error">{invalid(`section-${section.section_id}`)}</span>}</div>)}</fieldset>
    <fieldset disabled={locked}><legend>Typed connections ({draft.connections.length})</legend>{draft.connections.map((connection, index) => <ConnectionEditor key={connection.connection_id} campaignId={campaignId} revision={view.head_revision} connection={connection} error={invalid(`connection-${connection.connection_id}`)} onChange={(next) => update({ connections: draft.connections.map((item, itemIndex) => itemIndex === index ? next : item) })} onRemove={() => update({ connections: draft.connections.filter((_, itemIndex) => itemIndex !== index) })} />)}<button type="button" onClick={() => update({ connections: [...draft.connections, { connection_id: nextConnectionId(draft.connections), target_record_id: "", relationship: "connected-to", state: "current", context: "Describe this connection." }] })}>Add typed connection</button></fieldset>
    <div className="actions">{!proposal && !isCreate && mode !== "remove" && <button type="button" className="danger" disabled={locked} onClick={() => void startRemove()}>Load removal impact</button>}{!proposal && <button type="button" disabled={locked || mode === "remove"} onClick={() => { setMode("edit"); void save(); }}>{isCreate ? "Submit create proposal" : "Save as proposal"}</button>}{!proposal && mode === "remove" && <button type="button" disabled={busy} onClick={() => { setMode("edit"); setImpact(null); setResolutions([]); setMessage("Removal canceled."); }}>Cancel removal</button>}{!proposal && mode === "remove" && impact && <button type="button" disabled={locked || !removalReady} onClick={() => void save()}>Submit removal proposal</button>}{proposal && correctionMode && <><button type="button" disabled={busy || (proposal.mutation_kind === "remove" && !removalReady)} onClick={() => void submitCorrection()}>Submit correction/rebase</button><button type="button" disabled={busy} onClick={cancelCorrection}>Cancel correction</button></>}</div>{impact && mode === "remove" && <RemovalResolution campaignId={campaignId} revision={view.head_revision} impact={impact} resolutions={resolutions} setResolutions={setResolutions} disabled={locked} />}{proposal && <ProposalReview proposal={proposal} approve={() => { setWardenConfirmed(false); setApprovalDialog("approve"); }} reject={() => setApprovalDialog("reject")} startCorrection={startCorrection} correctionMode={correctionMode} busy={busy} />}</section>;
}

function ConnectionEditor({ campaignId, revision, connection, error, onChange, onRemove }: { campaignId: string; revision: RevisionRef; connection: EditorConnection; error?: string; onChange: (connection: EditorConnection) => void; onRemove: () => void }) { return <div className="editor-connection"><RecordPicker campaignId={campaignId} revision={revision} label={`Target for ${connection.connection_id}`} value={connection.target_record_id} onChange={(target_record_id) => onChange({ ...connection, target_record_id })} error={error} /><label htmlFor={`connection-relationship-${connection.connection_id}`}>Relationship</label><select id={`connection-relationship-${connection.connection_id}`} value={connection.relationship} onChange={(event) => onChange({ ...connection, relationship: event.target.value })}>{!relationships.includes(connection.relationship) && <option value={connection.relationship} disabled>Unsupported: {connection.relationship}</option>}{relationships.map((value) => <option key={value} value={value}>{value}</option>)}</select><label htmlFor={`connection-state-${connection.connection_id}`}>State</label><select id={`connection-state-${connection.connection_id}`} value={connection.state} onChange={(event) => onChange({ ...connection, state: event.target.value })}>{!connectionStates.includes(connection.state) && <option value={connection.state} disabled>Unsupported: {connection.state}</option>}{connectionStates.map((value) => <option key={value} value={value}>{value}</option>)}</select><label htmlFor={`connection-context-${connection.connection_id}`}>Context</label><textarea id={`connection-context-${connection.connection_id}`} rows={2} value={connection.context} onChange={(event) => onChange({ ...connection, context: event.target.value })} /><button type="button" onClick={onRemove}>Remove connection {connection.connection_id}</button></div>; }
function RemovalResolution({ campaignId, revision, impact, resolutions, setResolutions, disabled }: { campaignId: string; revision: RevisionRef; impact: EditorRemovalImpact; resolutions: Array<Record<string, unknown>>; setResolutions: (value: Array<Record<string, unknown>>) => void; disabled: boolean }) { return <section aria-labelledby="removal-impact-heading" className="editor-impact"><h3 id="removal-impact-heading">Removal impact and resolutions</h3><p>{impact.incoming_references.length} incoming typed connection(s) require a decision.</p>{impact.incoming_references.map((reference) => { const current = resolutions.find((item) => item.reference_id === reference.reference_id); const action = current?.action === "accept_unresolved" && !reference.permitted_unresolved ? "" : String(current?.action ?? ""); return <fieldset key={reference.reference_id} disabled={disabled}><legend>{reference.source_record_id} · {reference.relationship}</legend><label htmlFor={`resolution-${reference.reference_id}`}>Resolution for {reference.reference_id}</label><select id={`resolution-${reference.reference_id}`} required value={action} onChange={(event) => setResolutions(resolutions.map((item) => item.reference_id === reference.reference_id ? { reference_id: reference.reference_id, action: event.target.value, replacement_target_record_id: event.target.value === "redirect" ? "" : null } : item))}><option value="" disabled>Choose a resolution</option><option value="remove_reference">Remove reference</option><option value="redirect">Redirect reference</option>{reference.permitted_unresolved && <option value="accept_unresolved">Accept unresolved</option>}</select>{action === "redirect" && <RecordPicker campaignId={campaignId} revision={revision} label={`Replacement target for ${reference.reference_id}`} value={String(current?.replacement_target_record_id ?? "")} onChange={(target) => setResolutions(resolutions.map((item) => item.reference_id === reference.reference_id ? { ...item, replacement_target_record_id: target } : item))} />}</fieldset>; })}</section>; }
function ProposalReview({ proposal, approve, reject, startCorrection, correctionMode, busy }: { proposal: EditorProposal; approve: () => void; reject: () => void; startCorrection: () => void; correctionMode: boolean; busy: boolean }) { return <section className="editor-review" aria-labelledby="editor-review-heading"><h3 id="editor-review-heading">Exact proposal review</h3><p><strong>{proposal.diff.summary}</strong> · proposal <code>{proposal.proposal_id}</code>, version {proposal.proposal_version}</p><p>Base revision <code>{proposal.base_revision.revision_id}</code> · diff <code>{proposal.diff.diff_digest}</code></p><p>Validation: <strong>{proposal.validation.status}</strong> ({proposal.validation.error_count} errors)</p>{correctionMode && <p role="status">Editing a correction. The original proposal remains unchanged until the correction is submitted.</p>}{proposal.validation.findings.length > 0 && <ul>{proposal.validation.findings.map((finding) => <li key={finding.finding_id}>{finding.severity}: {finding.code} at {finding.location}</li>)}</ul>}<section className="diff" role="region" aria-labelledby="editor-diff-heading"><h4 id="editor-diff-heading">Exact field, section, and connection change cards</h4>{proposal.diff.cards.map((card, index) => <article key={String(card.change_id ?? index)} aria-labelledby={`editor-card-${index}`}><h5 id={`editor-card-${index}`}>{String(card.kind ?? "Change")} · {String(card.subject_record_id)}</h5><pre>{JSON.stringify(card, null, 2)}</pre></article>)}</section><div className="actions"><button type="button" disabled={busy || correctionMode} onClick={reject}>Reject exact proposal</button><button type="button" disabled={busy || correctionMode} onClick={startCorrection}>Create correction/rebase</button><button type="button" className="primary" disabled={busy || correctionMode || proposal.validation.status !== "passed" || proposal.validation.error_count !== 0} onClick={approve}>Approve and publish exact proposal</button></div></section>; }
