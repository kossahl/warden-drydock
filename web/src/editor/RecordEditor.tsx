import { useEffect, useRef, useState } from "react";
import { httpEditorApi, nextConnectionId, type EditorConnection, type EditorField, type EditorProposal, type EditorRecord, type EditorRecordView, type EditorRemovalImpact, type EditorSection, type RevisionRef } from "./editorClient";

const statuses = ["idea", "draft", "review", "canon", "revealed", "archived", "accepted"];
const authority = (status: string) => status === "canon" || status === "revealed" ? status : "preparation";
const clone = (record: EditorRecord): EditorRecord => ({ ...record, fields: record.fields.map((item) => ({ ...item })), sections: record.sections.map((item) => ({ ...item })), connections: record.connections.map((item) => ({ ...item })) });
const publicId = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : "request_failed";
const errorCategory = (reason: unknown) => reason && typeof reason === "object" && "category" in reason ? String((reason as { category?: unknown }).category ?? "") : "";
const staleCategories = ["stale_revision", "workflow_conflict", "stale_record_digest"];

export function RecordEditor({ campaignId, revisionId, recordId, navigate }: { campaignId: string; revisionId: string; recordId: string; navigate?: (href: string) => void }) {
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
  const [rejectionReason, setRejectionReason] = useState("review_rejected");
  const [conflict, setConflict] = useState(false);
  const [correctionMode, setCorrectionMode] = useState(false);
  const correctionDraft = useRef<EditorRecord | null>(null);
  const correctionResolutions = useRef<Array<Record<string, unknown>> | null>(null);
  const correctionView = useRef<EditorRecordView | null>(null);
  const correctionImpact = useRef<EditorRemovalImpact | null>(null);
  const correctionBase = useRef<{ view: EditorRecordView; impact: EditorRemovalImpact | null } | null>(null);
  const errorHeading = useRef<HTMLHeadingElement>(null);
  const dialogHeading = useRef<HTMLHeadingElement>(null);
  const focusEditorError = useRef(false);

  const load = () => {
    setError(""); setMessage(""); setConflict(false); setProposal(null); setImpact(null); setCorrectionMode(false); correctionDraft.current = null; correctionResolutions.current = null; correctionView.current = null; correctionImpact.current = null; correctionBase.current = null;
    const sourceRecordId = isCreate ? "campaign-main" : recordId;
    void httpEditorApi.read(campaignId, revisionId, sourceRecordId).then((value) => {
      setView(value);
      setDraft(isCreate ? { record_id: "new-record", record_type: "npc", displayed_name: "New record", status: "idea", authority: "preparation", visibility: { audience: "warden", warden_only: true }, fields: [{ field_id: "ownership", value: "campaign" }], sections: [{ section_id: "summary", body: "" }], connections: [], content_digest: "0".repeat(64) } : clone(value.record));
    }).catch((reason: unknown) => { focusEditorError.current = !document.activeElement?.closest("#atlas-content"); setError(`Editor unavailable (${errorText(reason)}).`); });
  };
  useEffect(load, [campaignId, revisionId, recordId]);
  useEffect(() => {
    if (!error || !focusEditorError.current) return;
    focusEditorError.current = false;
    if (document.activeElement?.closest("#atlas-content") && !document.activeElement?.closest(".editor")) return;
    errorHeading.current?.focus();
  }, [error]);
  useEffect(() => { if (approvalDialog) dialogHeading.current?.focus(); }, [approvalDialog]);
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
    draft.fields.forEach((field) => { if (!/^[a-z0-9][a-z0-9-]*$/.test(field.field_id)) next[`field-${field.field_id}`] = "Field ID is invalid."; });
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
  const startRemove = async () => {
    if (!view || !draft || !view.editable || !validate()) return;
    setBusy(true); setError(""); setMessage(""); setConflict(false);
    try { const value = await httpEditorApi.impact(campaignId, view.head_revision.revision_id, draft.record_id); setImpact(value); setMode("remove"); setResolutions(value.incoming_references.map((reference) => ({ reference_id: reference.reference_id, action: reference.permitted_unresolved ? "accept_unresolved" : "remove_reference", replacement_target_record_id: null }))); setMessage("Resolve every incoming typed connection before submitting removal."); }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`Removal impact unavailable (${errorText(reason)}).`); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!view || !draft || !view.editable || !validate()) return;
    setBusy(true); setError(""); setMessage(""); setConflict(false);
    try { const value = await httpEditorApi.propose(isCreate ? "create" : mode, campaignId, view.head_revision, draft, view.editor_workflow_version, resolutions, impact ?? undefined); setProposal(value); setCorrectionMode(false); setMessage("Exact proposal loaded for review. The current head is unchanged."); }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`Proposal was not created (${errorText(reason)}).`); }
    finally { setBusy(false); }
  };
  const submitDecision = async () => {
    if (!proposal || !approvalDialog || correctionMode) return;
    const approving = approvalDialog === "approve"; setBusy(true); setError(""); setConflict(false);
    try { const result = approving ? await httpEditorApi.approve(proposal) : await httpEditorApi.reject(proposal, rejectionReason); setApprovalDialog(null); if (approving) { setMessage("Proposal approved and published."); const revision = result.published_revision as RevisionRef | undefined; const createdRecordId = proposal.mutation_kind === "create" ? proposal.record_bindings[0]?.record_id : undefined; if (revision && navigate) navigate(createdRecordId ? `/campaigns/${encodeURIComponent(campaignId)}/records/${encodeURIComponent(createdRecordId)}?revision=${encodeURIComponent(revision.revision_id)}` : `/campaigns/${encodeURIComponent(campaignId)}?revision=${encodeURIComponent(revision.revision_id)}`); } else { setProposal(null); setCorrectionMode(false); setMessage("Proposal rejected. No campaign revision changed."); } }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`${approving ? "Approval" : "Rejection"} blocked (${errorText(reason)}). Refresh and review the current head.`); }
    finally { setBusy(false); }
  };
  const startCorrection = async () => {
    if (!proposal || busy) return;
    correctionDraft.current = draft ? clone(draft) : null;
    correctionResolutions.current = resolutions.map((resolution) => ({ ...resolution }));
    correctionView.current = view;
    correctionImpact.current = impact;
    setBusy(true); setFieldErrors({}); setError(""); setConflict(false);
    try {
      const proposalRecordId = proposal.record_bindings[0]?.record_id;
      const sourceRecordId = proposal.mutation_kind === "create" ? "campaign-main" : proposalRecordId ?? draft?.record_id ?? recordId;
      const base = await httpEditorApi.read(campaignId, proposal.base_revision.revision_id, sourceRecordId);
      const currentHead = base.viewed_revision.revision_id === base.head_revision.revision_id
        ? base
        : await httpEditorApi.read(campaignId, base.head_revision.revision_id, sourceRecordId);
      const currentImpact = proposal.mutation_kind === "remove"
        ? await httpEditorApi.impact(campaignId, currentHead.head_revision.revision_id, sourceRecordId)
        : null;
      correctionBase.current = { view: currentHead, impact: currentImpact };
      const rebasingStaleProposal = currentHead.head_revision.revision_id !== proposal.base_revision.revision_id;
      if (proposal.mutation_kind === "create" || !rebasingStaleProposal) {
        setDraft((current) => current && proposalRecordId ? { ...current, record_id: proposalRecordId } : current);
      } else {
        setDraft(clone(currentHead.record));
      }
      if (currentImpact) {
        setResolutions(currentImpact.incoming_references.map((reference) => resolutions.find((item) => item.reference_id === reference.reference_id) ?? { reference_id: reference.reference_id, action: reference.permitted_unresolved ? "accept_unresolved" : "remove_reference", replacement_target_record_id: null }));
      }
      setView(currentHead); setImpact(currentImpact); setCorrectionMode(true); setMessage("Correction mode: edit the candidate from the current head, then submit a new proposal version.");
    } catch (reason) {
      correctionDraft.current = null; correctionResolutions.current = null; correctionView.current = null; correctionImpact.current = null;
      setError(`Correction could not start (${errorText(reason)}). Reload the current head and try again.`); focusEditorError.current = true;
    } finally { setBusy(false); }
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
      setView(currentHead); setDraft(correctedDraft); setProposal(value); setImpact(currentImpact ?? null); setCorrectionMode(false); correctionDraft.current = null; correctionResolutions.current = null; correctionView.current = null; correctionImpact.current = null; correctionBase.current = null; setMessage("Correction created as a new immutable proposal version.");
    }
    catch (reason) { setConflict(staleCategories.includes(errorCategory(reason))); focusEditorError.current = true; setError(`Correction blocked (${errorText(reason)}). Reload the current head and rebase the fields.`); }
    finally { setBusy(false); }
  };
  const openCurrentHead = () => navigate?.(`/campaigns/${encodeURIComponent(campaignId)}/records/${encodeURIComponent(recordId)}`);

  if (approvalDialog && proposal) return <dialog open className="editor-dialog" aria-modal="true" aria-labelledby="editor-dialog-heading" onKeyDown={(event) => { if (event.key === "Escape") setApprovalDialog(null); }}><section role="document"><h2 id="editor-dialog-heading" ref={dialogHeading} tabIndex={-1}>{approvalDialog === "approve" ? "Approve exact proposal" : "Reject proposal"}</h2><p>Proposal <code>{proposal.proposal_id}</code>, version {proposal.proposal_version}. Base revision <code>{proposal.base_revision.revision_id}</code>.</p>{approvalDialog === "approve" ? <p>Confirm the displayed diff, validation result, authority transitions, visibility transitions, and affected record count before publishing.</p> : <label htmlFor="editor-rejection-reason">Reason code<input id="editor-rejection-reason" value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)} /></label>}<div className="actions"><button type="button" disabled={busy} onClick={() => setApprovalDialog(null)}>Cancel</button><button type="button" className="primary" disabled={busy || (approvalDialog === "reject" && !/^[a-z][a-z0-9_]+$/.test(rejectionReason))} onClick={() => void submitDecision()}>{approvalDialog === "approve" ? "Approve and publish" : "Reject exact proposal"}</button></div></section></dialog>;
  if (error && !view) return <section className="card editor" role="alert" aria-labelledby="editor-error-heading"><h2 id="editor-error-heading" ref={errorHeading} tabIndex={-1}>Record editor</h2><p>{error}</p><button type="button" onClick={load}>Retry</button></section>;
  if (!view || !draft) return <section className="card editor" aria-busy="true"><h2>Record editor</h2><p role="status">Loading structured record.</p></section>;
  if (!view.editable) return <section className="card editor" aria-labelledby="editor-heading"><h2 id="editor-heading">Record editor</h2><p>Historical revisions are read-only. Open the current head to propose a change.</p>{navigate && <button type="button" onClick={openCurrentHead}>Open current head</button>}</section>;

  const invalid = (key: string) => fieldErrors[key];
  const setField = (index: number, field: EditorField) => update({ fields: draft.fields.map((item, itemIndex) => itemIndex === index ? field : item) });
  const setSection = (index: number, section: EditorSection) => update({ sections: draft.sections.map((item, itemIndex) => itemIndex === index ? section : item) });
  const locked = busy || (!!proposal && !correctionMode);
  return <section className="card editor" aria-labelledby="editor-heading"><div className="section-title"><h2 id="editor-heading">{isCreate ? "Create record" : "Edit record"}</h2><span role="status">Head · workflow {view.editor_workflow_version}</span></div><p>Changes create a typed proposal. Approval is required before the campaign head changes.</p>{error && <div className="error editor-error" role="alert" aria-labelledby="editor-error-heading"><h3 id="editor-error-heading" ref={errorHeading} tabIndex={-1}>Editor error</h3><p>{error}</p></div>}{message && <p role="status" aria-live="polite">{message}</p>}{conflict && <aside className="editor-conflict" role="alert" aria-labelledby="editor-conflict-heading"><h3 id="editor-conflict-heading">Head changed; rebase required</h3><p>This proposal is bound to an older revision or workflow. Reload the current head before retrying.</p>{navigate && <button type="button" onClick={openCurrentHead}>Reload current head</button>}</aside>}
    <fieldset disabled={locked}><legend>Record details</legend><label htmlFor="editor-record-id">Record ID</label><input id="editor-record-id" value={draft.record_id} readOnly={!isCreate || (!!proposal && correctionMode)} onChange={(event) => update({ record_id: event.target.value })} aria-invalid={!!invalid("record_id")} aria-describedby={invalid("record_id") ? "editor-record-id-error" : undefined} />{invalid("record_id") && <span id="editor-record-id-error" className="error">{invalid("record_id")}</span>}<label htmlFor="editor-name">Displayed name</label><input id="editor-name" value={draft.displayed_name} onChange={(event) => update({ displayed_name: event.target.value })} aria-invalid={!!invalid("displayed_name")} aria-describedby={invalid("displayed_name") ? "editor-name-error" : undefined} />{invalid("displayed_name") && <span id="editor-name-error" className="error">{invalid("displayed_name")}</span>}<label htmlFor="editor-type">Record type</label><input id="editor-type" value={draft.record_type} readOnly={!isCreate} onChange={(event) => update({ record_type: event.target.value })} /><label htmlFor="editor-status">Status</label><select id="editor-status" value={draft.status} onChange={(event) => update({ status: event.target.value, authority: authority(event.target.value) as EditorRecord["authority"] })}>{statuses.map((value) => <option key={value} value={value}>{value}</option>)}</select><p>Authority: <strong>{authority(draft.status)}</strong> (derived from status)</p><label htmlFor="editor-visibility">Visibility</label><select id="editor-visibility" value={draft.visibility.audience} onChange={(event) => update({ visibility: event.target.value === "warden" ? { audience: "warden", warden_only: true } : { audience: event.target.value as "players" | "shared", warden_only: false } })}><option value="warden">Warden only</option><option value="shared">Shared</option><option value="players">Players</option></select></fieldset>
    <fieldset disabled={locked}><legend>Fields</legend>{draft.fields.map((field, index) => <div key={field.field_id}><label htmlFor={`editor-field-${field.field_id}`}>{field.field_id}</label><input id={`editor-field-${field.field_id}`} value={String(field.value ?? "")} onChange={(event) => setField(index, { ...field, value: event.target.value })} aria-invalid={!!invalid(`field-${field.field_id}`)} />{invalid(`field-${field.field_id}`) && <span className="error">{invalid(`field-${field.field_id}`)}</span>}<button type="button" onClick={() => update({ fields: draft.fields.filter((_, itemIndex) => itemIndex !== index) })}>Remove field {field.field_id}</button></div>)}<button type="button" onClick={() => update({ fields: [...draft.fields, { field_id: `field-${draft.fields.length + 1}`, value: "" }] })}>Add field</button></fieldset>
    <fieldset disabled={locked}><legend>Content sections</legend>{draft.sections.map((section, index) => <div key={section.section_id}><label htmlFor={`editor-section-${section.section_id}`}>{section.section_id}</label><textarea id={`editor-section-${section.section_id}`} rows={5} value={section.body} onChange={(event) => setSection(index, { ...section, body: event.target.value })} aria-invalid={!!invalid(`section-${section.section_id}`)} />{invalid(`section-${section.section_id}`) && <span className="error">{invalid(`section-${section.section_id}`)}</span>}<button type="button" onClick={() => update({ sections: draft.sections.filter((_, itemIndex) => itemIndex !== index) })}>Remove section {section.section_id}</button></div>)}<button type="button" onClick={() => update({ sections: [...draft.sections, { section_id: `section-${draft.sections.length + 1}`, body: "" }] })}>Add content section</button></fieldset>
    <fieldset disabled={locked}><legend>Typed connections ({draft.connections.length})</legend>{draft.connections.map((connection, index) => <ConnectionEditor key={connection.connection_id} connection={connection} error={invalid(`connection-${connection.connection_id}`)} onChange={(next) => update({ connections: draft.connections.map((item, itemIndex) => itemIndex === index ? next : item) })} onRemove={() => update({ connections: draft.connections.filter((_, itemIndex) => itemIndex !== index) })} />)}<button type="button" onClick={() => update({ connections: [...draft.connections, { connection_id: nextConnectionId(draft.connections), target_record_id: "", relationship: "related-to", state: "current", context: "Describe this connection." }] })}>Add typed connection</button></fieldset>
    <div className="actions">{!proposal && !isCreate && <button type="button" className="danger" disabled={locked} onClick={() => void startRemove()}>Load removal impact</button>}{!proposal && <button type="button" disabled={locked || mode === "remove"} onClick={() => { setMode("edit"); void save(); }}>{isCreate ? "Submit create proposal" : "Save as proposal"}</button>}{!proposal && mode === "remove" && impact && <button type="button" disabled={locked} onClick={() => void save()}>Submit removal proposal</button>}{proposal && correctionMode && <><button type="button" disabled={busy} onClick={() => void submitCorrection()}>Submit correction/rebase</button><button type="button" disabled={busy} onClick={cancelCorrection}>Cancel correction</button></>}</div>{impact && mode === "remove" && <RemovalResolution impact={impact} resolutions={resolutions} setResolutions={setResolutions} disabled={locked} />}{proposal && <ProposalReview proposal={proposal} approve={() => setApprovalDialog("approve")} reject={() => setApprovalDialog("reject")} startCorrection={startCorrection} correctionMode={correctionMode} busy={busy} />}</section>;
}

function ConnectionEditor({ connection, error, onChange, onRemove }: { connection: EditorConnection; error?: string; onChange: (connection: EditorConnection) => void; onRemove: () => void }) { return <div className="editor-connection"><label htmlFor={`connection-target-${connection.connection_id}`}>Target for {connection.connection_id}</label><input id={`connection-target-${connection.connection_id}`} value={connection.target_record_id} onChange={(event) => onChange({ ...connection, target_record_id: event.target.value })} aria-invalid={!!error} />{error && <span className="error" role="alert">{error}</span>}<label htmlFor={`connection-relationship-${connection.connection_id}`}>Relationship</label><input id={`connection-relationship-${connection.connection_id}`} value={connection.relationship} onChange={(event) => onChange({ ...connection, relationship: event.target.value })} /><label htmlFor={`connection-state-${connection.connection_id}`}>State</label><input id={`connection-state-${connection.connection_id}`} value={connection.state} onChange={(event) => onChange({ ...connection, state: event.target.value })} /><label htmlFor={`connection-context-${connection.connection_id}`}>Context</label><textarea id={`connection-context-${connection.connection_id}`} rows={2} value={connection.context} onChange={(event) => onChange({ ...connection, context: event.target.value })} /><button type="button" onClick={onRemove}>Remove connection {connection.connection_id}</button></div>; }
function RemovalResolution({ impact, resolutions, setResolutions, disabled }: { impact: EditorRemovalImpact; resolutions: Array<Record<string, unknown>>; setResolutions: (value: Array<Record<string, unknown>>) => void; disabled: boolean }) { return <section aria-labelledby="removal-impact-heading" className="editor-impact"><h3 id="removal-impact-heading">Removal impact and resolutions</h3><p>{impact.incoming_references.length} incoming typed connection(s) require a decision.</p>{impact.incoming_references.map((reference) => { const current = resolutions.find((item) => item.reference_id === reference.reference_id); const action = String(current?.action ?? "remove_reference"); return <fieldset key={reference.reference_id} disabled={disabled}><legend>{reference.source_record_id} · {reference.relationship}</legend><label htmlFor={`resolution-${reference.reference_id}`}>Resolution for {reference.reference_id}</label><select id={`resolution-${reference.reference_id}`} value={action} onChange={(event) => setResolutions(resolutions.map((item) => item.reference_id === reference.reference_id ? { reference_id: reference.reference_id, action: event.target.value, replacement_target_record_id: event.target.value === "redirect" ? "" : null } : item))}><option value="remove_reference">Remove reference</option><option value="redirect">Redirect reference</option>{reference.permitted_unresolved && <option value="accept_unresolved">Accept unresolved</option>}</select>{action === "redirect" && <input aria-label={`Replacement target for ${reference.reference_id}`} value={String(current?.replacement_target_record_id ?? "")} onChange={(event) => setResolutions(resolutions.map((item) => item.reference_id === reference.reference_id ? { ...item, replacement_target_record_id: event.target.value } : item))} />}</fieldset>; })}</section>; }
function ProposalReview({ proposal, approve, reject, startCorrection, correctionMode, busy }: { proposal: EditorProposal; approve: () => void; reject: () => void; startCorrection: () => void; correctionMode: boolean; busy: boolean }) { return <section className="editor-review" aria-labelledby="editor-review-heading"><h3 id="editor-review-heading">Exact proposal review</h3><p><strong>{proposal.diff.summary}</strong> · proposal <code>{proposal.proposal_id}</code>, version {proposal.proposal_version}</p><p>Base revision <code>{proposal.base_revision.revision_id}</code> · diff <code>{proposal.diff.diff_digest}</code></p><p>Validation: <strong>{proposal.validation.status}</strong> ({proposal.validation.error_count} errors)</p>{correctionMode && <p role="status">Editing a correction. The original proposal remains unchanged until the correction is submitted.</p>}{proposal.validation.findings.length > 0 && <ul>{proposal.validation.findings.map((finding) => <li key={finding.finding_id}>{finding.severity}: {finding.code} at {finding.location}</li>)}</ul>}<section className="diff" role="region" aria-labelledby="editor-diff-heading"><h4 id="editor-diff-heading">Exact field, section, and connection change cards</h4>{proposal.diff.cards.map((card, index) => <article key={String(card.change_id ?? index)} aria-labelledby={`editor-card-${index}`}><h5 id={`editor-card-${index}`}>{String(card.kind ?? "Change")} · {String(card.subject_record_id)}</h5><pre>{JSON.stringify(card, null, 2)}</pre></article>)}</section><div className="actions"><button type="button" disabled={busy || correctionMode} onClick={reject}>Reject exact proposal</button><button type="button" disabled={busy || correctionMode} onClick={startCorrection}>Create correction/rebase</button><button type="button" className="primary" disabled={busy || correctionMode || proposal.validation.status !== "passed" || proposal.validation.error_count !== 0} onClick={approve}>Approve and publish exact proposal</button></div></section>; }
