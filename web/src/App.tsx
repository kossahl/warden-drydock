import { AuthorityBadge, RevisionStatus, SyncStatus } from "./components/StatusPrimitives";
import type { RevisionRef } from "./contracts/v1";

const viewed: RevisionRef = {
  revision_id: "revision_12",
  ordinal: 12,
  tree_digest: "a".repeat(64),
};

export function App() {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="banner">
        <div>
          <p className="eyebrow">Local Warden workspace</p>
          <strong>Warden Drydock</strong>
        </div>
        <span role="status">Provider: Setup required</span>
      </header>
      <nav aria-label="Primary" className="primary-nav">
        <a aria-current="page" href="/campaigns/campaign_alpha/atlas">Atlas</a>
        <a href="/campaigns/campaign_alpha/prepare">Prepare</a>
        <a href="/campaigns/campaign_alpha/live">Live</a>
        <a href="/campaigns/campaign_alpha/proposals">Proposals</a>
        <a href="/campaigns/campaign_alpha/revisions">Revisions</a>
      </nav>
      <aside aria-label="Campaign authority and synchronization" className="authority-strip">
        <RevisionStatus viewed={viewed} head="revision_12" />
        <AuthorityBadge authority="canon" />
        <SyncStatus state="Synced" />
      </aside>
      <main id="main-content" tabIndex={-1}>
        <p className="eyebrow">Synthetic Campaign · Mothership</p>
        <h1>Campaign Atlas</h1>
        <section className="empty-state" aria-labelledby="records-heading">
          <h2 id="records-heading">Records</h2>
          <p>No records are available at this revision.</p>
        </section>
      </main>
      <div className="announcer" aria-live="polite" aria-atomic="true" />
    </div>
  );
}
