import type { Authority, DraftProvenance, RevisionRef, SaveSyncState } from "../contracts/v2";

const authorityLabels: Record<Authority | "draft" | "proposal", string> = {
  preparation: "Preparation",
  table_fact: "Confirmed table fact",
  canon: "Canon",
  revealed: "Revealed",
  draft: "Draft",
  proposal: "Proposal",
};

export function AuthorityBadge({ authority }: { authority: Authority | "draft" | "proposal" }) {
  return <span className={`badge badge--${authority}`}>{authorityLabels[authority]}</span>;
}

export function RevisionStatus({ viewed, head }: { viewed: RevisionRef; head: string }) {
  const atHead = viewed.revision_id === head;
  return (
    <span className="revision-status">
      Viewed revision {viewed.ordinal}{atHead ? " · Head" : ` · Head is ${head}`}
    </span>
  );
}

export function SyncStatus({ state }: { state: SaveSyncState }) {
  return <span className="sync-status" role="status">{state}</span>;
}

export function Provenance({ value }: { value: DraftProvenance }) {
  return (
    <section aria-labelledby="provenance-heading" className="provenance">
      <h2 id="provenance-heading">Sources and provenance</h2>
      <p><AuthorityBadge authority="draft" /> Grounded at revision {value.revision.ordinal}</p>
      <ul>
        {value.sources.map((source) => (
          <li key={source.source_id}>
            {source.label} · {source.authority} · {source.revision_id}
          </li>
        ))}
      </ul>
    </section>
  );
}
